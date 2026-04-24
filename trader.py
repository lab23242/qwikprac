"""
Transaction builder and submitter.
Supports both Jito bundle submission and standard RPC.
Uses high priority fees for fastest possible inclusion.
"""
import asyncio
import base64
import logging
import random
import struct
from typing import Optional

import aiohttp
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.system_program import transfer, TransferParams
from solders.transaction import VersionedTransaction
from solders.message import MessageV0
from solders.hash import Hash
from solders.instruction import Instruction

from config import (
    RPC_URL,
    JITO_BLOCK_ENGINE_URL,
    USE_JITO,
    BUY_AMOUNT_SOL,
    SLIPPAGE,
    PRIORITY_FEE_MICROLAMPORTS,
    JITO_TIP_SOL,
    LAMPORTS_PER_SOL,
)
from pumpfun import (
    BondingCurve,
    build_buy_instruction,
    get_bonding_curve_pda,
    JITO_TIP_ACCOUNTS,
)

log = logging.getLogger(__name__)


async def snipe(
    keypair: Keypair,
    mint: Pubkey,
    bonding_curve: Pubkey,
    bc_data: Optional[BondingCurve],
    session: aiohttp.ClientSession,
) -> Optional[str]:
    """
    Build and submit a buy transaction. Returns tx signature on success.
    bc_data may be None if we couldn't fetch it yet; we fall back to a
    conservative token estimate.
    """
    blockhash = await _get_recent_blockhash(session)
    if not blockhash:
        log.error("Could not fetch blockhash")
        return None

    sol_in = BUY_AMOUNT_SOL
    if bc_data:
        token_amount = bc_data.tokens_for_sol(sol_in)
    else:
        # Rough estimate: assume price ~0.000000028 SOL/token at launch
        token_amount = int(sol_in / 0.000000028)

    max_sol_cost = int(sol_in * LAMPORTS_PER_SOL * (1 + SLIPPAGE))

    buy_ix = build_buy_instruction(
        buyer=keypair.pubkey(),
        mint=mint,
        bonding_curve=bonding_curve,
        token_amount=token_amount,
        max_sol_cost=max_sol_cost,
    )

    instructions: list[Instruction] = [
        set_compute_unit_price(PRIORITY_FEE_MICROLAMPORTS),
        set_compute_unit_limit(200_000),
        buy_ix,
    ]

    if USE_JITO:
        tip_account = random.choice(JITO_TIP_ACCOUNTS)
        tip_lamports = int(JITO_TIP_SOL * LAMPORTS_PER_SOL)
        tip_ix = transfer(TransferParams(
            from_pubkey=keypair.pubkey(),
            to_pubkey=tip_account,
            lamports=tip_lamports,
        ))
        instructions.append(tip_ix)

    try:
        msg = MessageV0.try_compile(
            payer=keypair.pubkey(),
            instructions=instructions,
            address_lookup_table_accounts=[],
            recent_blockhash=Hash.from_string(blockhash),
        )
        tx = VersionedTransaction(msg, [keypair])
    except Exception as exc:
        log.error("Transaction build failed: %s", exc)
        return None

    tx_bytes = bytes(tx)

    if USE_JITO:
        sig = await _submit_jito(tx_bytes, session)
        if sig:
            log.info("Jito bundle submitted: %s", sig)
            return sig
        log.warning("Jito submission failed; falling back to RPC")

    sig = await _submit_rpc(tx_bytes, session)
    if sig:
        log.info("RPC submission succeeded: %s", sig)
    return sig


async def _get_recent_blockhash(session: aiohttp.ClientSession) -> Optional[str]:
    payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "getLatestBlockhash",
        "params": [{"commitment": "processed"}],
    }
    try:
        async with session.post(RPC_URL, json=payload, timeout=aiohttp.ClientTimeout(total=5)) as r:
            data = await r.json()
            return data["result"]["value"]["blockhash"]
    except Exception as exc:
        log.error("getLatestBlockhash error: %s", exc)
        return None


async def _submit_rpc(tx_bytes: bytes, session: aiohttp.ClientSession) -> Optional[str]:
    encoded = base64.b64encode(tx_bytes).decode()
    payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "sendTransaction",
        "params": [encoded, {
            "encoding": "base64",
            "skipPreflight": True,
            "maxRetries": 3,
            "preflightCommitment": "processed",
        }],
    }
    try:
        async with session.post(RPC_URL, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as r:
            data = await r.json()
            if "error" in data:
                log.error("RPC sendTransaction error: %s", data["error"])
                return None
            return data.get("result")
    except Exception as exc:
        log.error("RPC sendTransaction exception: %s", exc)
        return None


async def _submit_jito(tx_bytes: bytes, session: aiohttp.ClientSession) -> Optional[str]:
    encoded = base64.b64encode(tx_bytes).decode()
    payload = {"jsonrpc": "2.0", "id": 1, "method": "sendBundle", "params": [[encoded]]}
    try:
        async with session.post(
            JITO_BLOCK_ENGINE_URL,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=8),
        ) as r:
            data = await r.json()
            if "error" in data:
                log.warning("Jito error: %s", data["error"])
                return None
            return data.get("result")
    except Exception as exc:
        log.warning("Jito submission exception: %s", exc)
        return None


async def fetch_bonding_curve(
    mint: Pubkey, session: aiohttp.ClientSession
) -> Optional[BondingCurve]:
    """Fetch and decode the bonding curve account for a mint."""
    import base64 as _b64
    bc_pda = get_bonding_curve_pda(mint)
    payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "getAccountInfo",
        "params": [str(bc_pda), {"encoding": "base64", "commitment": "processed"}],
    }
    try:
        async with session.post(RPC_URL, json=payload, timeout=aiohttp.ClientTimeout(total=4)) as r:
            data = await r.json()
            raw_list = data["result"]["value"]["data"]
            bc_bytes = _b64.b64decode(raw_list[0])
            return BondingCurve.decode(bc_bytes)
    except Exception:
        return None
