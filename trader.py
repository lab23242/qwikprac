"""
Transaction builder, blockhash cache, and submitters (Jito + RPC fallback).
BlockhashCache proactively refreshes every 8 s so snipe() never waits for an
RPC round-trip to get a blockhash.
"""
import asyncio
import base64
import logging
import random
import struct
from typing import Optional

import aiohttp
import orjson
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
from pumpfun import BondingCurve, build_buy_instruction, get_bonding_curve_pda, JITO_TIP_ACCOUNTS

log = logging.getLogger(__name__)

_BH_PAYLOAD = orjson.dumps({
    "jsonrpc": "2.0", "id": 1,
    "method": "getLatestBlockhash",
    "params": [{"commitment": "processed"}],
})


class BlockhashCache:
    """
    Keeps a fresh recent-blockhash in memory.
    Call .start(session) once from main() to begin background refreshing.
    snipe() reads _hash directly — zero extra RPC latency on the buy path.
    """
    _REFRESH_INTERVAL = 8  # seconds; blockhash valid ~60 s

    def __init__(self) -> None:
        self._hash: str | None = None

    def start(self, session: aiohttp.ClientSession) -> None:
        asyncio.create_task(self._loop(session))

    async def get(self, session: aiohttp.ClientSession) -> str | None:
        if self._hash:
            return self._hash
        return await _fetch_blockhash(session)

    async def _loop(self, session: aiohttp.ClientSession) -> None:
        while True:
            try:
                h = await _fetch_blockhash(session)
                if h:
                    self._hash = h
            except Exception as exc:
                log.debug("blockhash refresh: %s", exc)
            await asyncio.sleep(self._REFRESH_INTERVAL)


BLOCKHASH_CACHE = BlockhashCache()


async def snipe(
    keypair: Keypair,
    mint: Pubkey,
    bonding_curve: Pubkey,
    bc_data: Optional[BondingCurve],
    session: aiohttp.ClientSession,
    creator: Optional[Pubkey] = None,
) -> Optional[str]:
    blockhash = await BLOCKHASH_CACHE.get(session)
    if not blockhash:
        log.error("Could not obtain blockhash")
        return None

    sol_in = BUY_AMOUNT_SOL
    if bc_data:
        token_amount = bc_data.tokens_for_sol(sol_in)
    else:
        # Conservative estimate at pump.fun launch price
        token_amount = int(sol_in * 1e9 / 30e9 * 1_073_000_000_000_000)

    max_sol_cost = int(sol_in * LAMPORTS_PER_SOL * (1 + SLIPPAGE))

    if creator is None:
        log.warning("snipe() called without creator — creator vault PDA will be wrong")
        creator = keypair.pubkey()   # placeholder to avoid hard crash

    instructions: list[Instruction] = [
        set_compute_unit_price(PRIORITY_FEE_MICROLAMPORTS),
        set_compute_unit_limit(200_000),
        build_buy_instruction(
            buyer=keypair.pubkey(),
            mint=mint,
            bonding_curve=bonding_curve,
            token_amount=token_amount,
            max_sol_cost=max_sol_cost,
            creator=creator,
        ),
    ]

    if USE_JITO:
        instructions.append(transfer(TransferParams(
            from_pubkey=keypair.pubkey(),
            to_pubkey=random.choice(JITO_TIP_ACCOUNTS),
            lamports=int(JITO_TIP_SOL * LAMPORTS_PER_SOL),
        )))

    try:
        msg = MessageV0.try_compile(
            payer=keypair.pubkey(),
            instructions=instructions,
            address_lookup_table_accounts=[],
            recent_blockhash=Hash.from_string(blockhash),
        )
        tx_bytes = bytes(VersionedTransaction(msg, [keypair]))
    except Exception as exc:
        log.error("Transaction build failed: %s", exc)
        return None

    if USE_JITO:
        sig = await _submit_jito(tx_bytes, session)
        if sig:
            return sig
        log.warning("Jito failed; falling back to RPC")

    return await _submit_rpc(tx_bytes, session)


async def fetch_bonding_curve(mint: Pubkey, session: aiohttp.ClientSession) -> Optional[BondingCurve]:
    bc_pda = get_bonding_curve_pda(mint)
    payload = orjson.dumps({
        "jsonrpc": "2.0", "id": 1,
        "method": "getAccountInfo",
        "params": [str(bc_pda), {"encoding": "base64", "commitment": "processed"}],
    })
    try:
        async with session.post(
            RPC_URL, data=payload,
            headers={"Content-Type": "application/json"},
            timeout=aiohttp.ClientTimeout(total=4),
        ) as r:
            data = orjson.loads(await r.read())
            bc_bytes = base64.b64decode(data["result"]["value"]["data"][0])
            return BondingCurve.decode(bc_bytes)
    except Exception:
        return None


async def _fetch_blockhash(session: aiohttp.ClientSession) -> Optional[str]:
    try:
        async with session.post(
            RPC_URL, data=_BH_PAYLOAD,
            headers={"Content-Type": "application/json"},
            timeout=aiohttp.ClientTimeout(total=5),
        ) as r:
            data = orjson.loads(await r.read())
            return data["result"]["value"]["blockhash"]
    except Exception as exc:
        log.debug("getLatestBlockhash error: %s", exc)
        return None


async def _submit_rpc(tx_bytes: bytes, session: aiohttp.ClientSession) -> Optional[str]:
    encoded = base64.b64encode(tx_bytes).decode()
    payload = orjson.dumps({
        "jsonrpc": "2.0", "id": 1,
        "method": "sendTransaction",
        "params": [encoded, {
            "encoding": "base64",
            "skipPreflight": True,
            "maxRetries": 3,
            "preflightCommitment": "processed",
        }],
    })
    try:
        async with session.post(
            RPC_URL, data=payload,
            headers={"Content-Type": "application/json"},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as r:
            data = orjson.loads(await r.read())
            if "error" in data:
                log.error("RPC sendTransaction error: %s", data["error"])
                return None
            sig = data.get("result")
            if sig:
                log.info("RPC sendTransaction: %s", sig)
            return sig
    except Exception as exc:
        log.error("RPC sendTransaction exception: %s", exc)
        return None


async def _submit_jito(tx_bytes: bytes, session: aiohttp.ClientSession) -> Optional[str]:
    encoded = base64.b64encode(tx_bytes).decode()
    payload = orjson.dumps({"jsonrpc": "2.0", "id": 1, "method": "sendBundle", "params": [[encoded]]})
    try:
        async with session.post(
            JITO_BLOCK_ENGINE_URL, data=payload,
            headers={"Content-Type": "application/json"},
            timeout=aiohttp.ClientTimeout(total=8),
        ) as r:
            data = orjson.loads(await r.read())
            if "error" in data:
                log.warning("Jito error: %s", data["error"])
                return None
            sig = data.get("result")
            if sig:
                log.info("Jito bundle: %s", sig)
            return sig
    except Exception as exc:
        log.warning("Jito exception: %s", exc)
        return None
