"""
Developer analysis: migration rate + token count for a creator wallet,
and social-media link check from token metadata URI.
Results are TTL-cached to avoid redundant RPC calls on the critical path.

Key fix: getSignaturesForAddress queries the CREATOR address (not the pump.fun
program), then JSON-RPC batches getTransaction to find pump.fun creates.
Migration checks use getMultipleAccounts (one round-trip per 100 tokens).
"""
import asyncio
import base64
import logging
import time
from dataclasses import dataclass
from typing import Optional

import aiohttp
import base58
import orjson

from config import RPC_URL
from pumpfun import PUMP_PROGRAM_ID, CREATE_DISCRIMINATOR, BondingCurve, get_bonding_curve_pda

log = logging.getLogger(__name__)

_dev_cache:  dict[str, tuple[float, "DevStats"]] = {}
_meta_cache: dict[str, tuple[float, bool]]       = {}
_DEV_CACHE_TTL  = 300   # seconds
_META_CACHE_TTL = 60


@dataclass
class DevStats:
    tokens_launched: int  = 0
    tokens_migrated: int  = 0
    migration_rate:  float = 0.0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def get_dev_stats(creator: str, session: aiohttp.ClientSession) -> DevStats:
    now    = time.monotonic()
    cached = _dev_cache.get(creator)
    if cached and (now - cached[0]) < _DEV_CACHE_TTL:
        return cached[1]
    stats = await _fetch_dev_stats(creator, session)
    _dev_cache[creator] = (now, stats)
    return stats


async def has_social_media(uri: str, session: aiohttp.ClientSession) -> bool:
    if not uri:
        return False
    now    = time.monotonic()
    cached = _meta_cache.get(uri)
    if cached and (now - cached[0]) < _META_CACHE_TTL:
        return cached[1]
    result = await _fetch_social(uri, session)
    _meta_cache[uri] = (now, result)
    return result


# ---------------------------------------------------------------------------
# Developer stats
# ---------------------------------------------------------------------------

async def _fetch_dev_stats(creator: str, session: aiohttp.ClientSession) -> DevStats:
    sigs = await _get_creator_signatures(creator, session)
    if not sigs:
        return DevStats()
    mints = await _extract_created_mints(creator, sigs, session)
    if not mints:
        return DevStats()
    migrated = await _count_migrations_batch(mints, session)
    rate = migrated / len(mints)
    return DevStats(tokens_launched=len(mints), tokens_migrated=migrated, migration_rate=rate)


async def _get_creator_signatures(creator: str, session: aiohttp.ClientSession) -> list[str]:
    """
    Fetch the creator's own transaction history.
    Previously this queried PUMP_PROGRAM_ID (returning arbitrary users' txns).
    """
    payload = orjson.dumps({
        "jsonrpc": "2.0", "id": 1,
        "method": "getSignaturesForAddress",
        "params": [creator, {"limit": 1000, "commitment": "confirmed"}],
    })
    try:
        async with session.post(
            RPC_URL, data=payload,
            headers={"Content-Type": "application/json"},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as r:
            body = await r.read()
            if r.status != 200:
                log.debug("getSignaturesForAddress HTTP %d for %s", r.status, creator[:8])
                return []
            data = orjson.loads(body)
            return [s["signature"] for s in data.get("result", []) if not s.get("err")]
    except Exception as exc:
        log.warning("getSignaturesForAddress failed for %s: %s", creator[:8], exc)
        return []


async def _extract_created_mints(
    creator: str, signatures: list[str], session: aiohttp.ClientSession
) -> list[str]:
    """Batch-fetch transactions (JSON-RPC batch) and extract pump.fun create mints."""
    mints: list[str] = []
    batch_size = 50  # keep request size manageable

    for i in range(0, min(len(signatures), 500), batch_size):
        batch = signatures[i:i + batch_size]
        rpc_batch = [
            {
                "jsonrpc": "2.0", "id": idx,
                "method": "getTransaction",
                "params": [
                    sig,
                    {"encoding": "jsonParsed", "commitment": "confirmed",
                     "maxSupportedTransactionVersion": 0},
                ],
            }
            for idx, sig in enumerate(batch)
        ]
        try:
            async with session.post(
                RPC_URL, data=orjson.dumps(rpc_batch),
                headers={"Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=20),
            ) as r:
                results = orjson.loads(await r.read())
                for item in results:
                    tx = item.get("result")
                    if tx:
                        m = _extract_mint(tx, creator)
                        if m:
                            mints.append(m)
        except Exception as exc:
            log.debug("batch getTransaction error: %s", exc)

    return mints


def _extract_mint(tx: dict, creator: str) -> Optional[str]:
    """
    Return the mint pubkey if this transaction is a pump.fun create by `creator`.
    Discriminator check prevents matching buy/sell instructions.
    Mint is at account index 0 in the create instruction (not index 2 = bondingCurve).
    """
    try:
        if tx.get("meta", {}).get("err"):
            return None
        msg = tx["transaction"]["message"]
        if msg["accountKeys"][0]["pubkey"] != creator:
            return None
        pump_str = str(PUMP_PROGRAM_ID)
        for ix in msg.get("instructions", []):
            if ix.get("programId") == pump_str:
                m = _check_create_ix(ix)
                if m:
                    return m
        for group in tx.get("meta", {}).get("innerInstructions", []):
            for ix in group.get("instructions", []):
                if ix.get("programId") == pump_str:
                    m = _check_create_ix(ix)
                    if m:
                        return m
    except (KeyError, IndexError, TypeError):
        pass
    return None


def _check_create_ix(ix: dict) -> Optional[str]:
    """Return mint pubkey if ix is a pump.fun 'create' instruction, else None."""
    try:
        raw = base58.b58decode(ix.get("data", ""))
        if len(raw) < 8 or raw[:8] != CREATE_DISCRIMINATOR:
            return None
        accs = ix.get("accounts", [])
        return accs[0] if accs else None
    except Exception:
        return None


async def _count_migrations_batch(mints: list[str], session: aiohttp.ClientSession) -> int:
    """
    Check migration status for all mints in one getMultipleAccounts call per 100.
    complete=True or account closed both count as migrated.
    """
    from solders.pubkey import Pubkey
    pdas = [str(get_bonding_curve_pda(Pubkey.from_string(m))) for m in mints]
    migrated = 0

    for i in range(0, len(pdas), 100):
        batch_pdas = pdas[i:i + 100]
        payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": "getMultipleAccounts",
            "params": [batch_pdas, {"encoding": "base64", "commitment": "confirmed"}],
        }
        try:
            async with session.post(
                RPC_URL, json=payload, timeout=aiohttp.ClientTimeout(total=15)
            ) as r:
                data    = orjson.loads(await r.read())
                accounts = data["result"]["value"]
                for acct in accounts:
                    if acct is None:
                        migrated += 1          # account closed → migrated
                    else:
                        bc_bytes = base64.b64decode(acct["data"][0])
                        bc = BondingCurve.decode(bc_bytes)
                        if bc and bc.complete:
                            migrated += 1
        except Exception as exc:
            log.warning("getMultipleAccounts migration check failed: %s", exc)

    return migrated


# ---------------------------------------------------------------------------
# Social media
# ---------------------------------------------------------------------------

async def _fetch_social(uri: str, session: aiohttp.ClientSession) -> bool:
    url = _resolve_uri(uri)
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as r:
            if r.status != 200:
                return False
            meta = orjson.loads(await r.read())
            return any(meta.get(f) for f in ("twitter", "telegram", "website", "discord", "x"))
    except Exception as exc:
        log.debug("metadata fetch failed for %s: %s", uri, exc)
        return False


def _resolve_uri(uri: str) -> str:
    if uri.startswith("ipfs://"):
        return "https://ipfs.io/ipfs/" + uri[7:]
    return uri
