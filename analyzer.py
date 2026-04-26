"""
Developer analysis: migration rate + token count for a creator wallet,
and social-media link check from token metadata URI.
Results are TTL-cached to avoid redundant RPC calls on the critical path.

Historical creates are detected via two methods:
1. getProgramAccounts with memcmp filter at offset 81 (creator field in bonding
   curve accounts) — finds ALL bonding curves for a creator regardless of RPC
   history depth, no transaction log scanning needed.
2. Log-based fallback: getSignaturesForAddress + CreateEvent log parsing for
   very recent tokens whose bonding curve might not yet be indexed.

Migration checks use getMultipleAccounts (one round-trip per 100 tokens).
"""
import asyncio
import base64
import logging
import time
from dataclasses import dataclass
from typing import Optional

import aiohttp
import orjson
from solders.pubkey import Pubkey

from config import RPC_URL
from pumpfun import (
    BondingCurve, CreateEvent, CREATE_EVENT_DISCRIMINATOR,
    get_bonding_curve_pda, PUMP_PROGRAM_ID, BONDING_CURVE_CREATOR_OFFSET,
)

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
    # Primary: getProgramAccounts filtered by creator pubkey at bonding curve offset 81.
    # Returns ALL bonding curves (active + complete-but-not-closed) for this creator
    # without any RPC history depth limit.
    pga_result = await _get_bonding_curves_via_program_accounts(creator, session)

    # Secondary: recent tx log scanning catches tokens from the last few minutes
    # whose bonding curve might not yet be indexed, or RPC nodes that don't support
    # getProgramAccounts filters.
    mints_from_logs = await _get_mints_via_logs(creator, session)

    # Tally from getProgramAccounts: pga_result maps pda_str -> is_complete
    total_pga   = len(pga_result)
    migrated_pga = sum(1 for complete in pga_result.values() if complete)

    # From log-based mints: only count ones NOT already covered by getProgramAccounts
    # (those PDA strings we already have). For new ones, look them up.
    pga_pdas = set(pga_result.keys())
    new_mints_from_logs: list[str] = []
    for mint_str in mints_from_logs:
        try:
            pda_str = str(get_bonding_curve_pda(Pubkey.from_string(mint_str)))
        except Exception:
            continue
        if pda_str not in pga_pdas:
            new_mints_from_logs.append(mint_str)

    extra_migrated = 0
    if new_mints_from_logs:
        extra_migrated = await _count_migrations_batch(new_mints_from_logs, session)

    total    = total_pga + len(new_mints_from_logs)
    migrated = migrated_pga + extra_migrated

    if total == 0:
        return DevStats()

    rate = migrated / total
    return DevStats(tokens_launched=total, tokens_migrated=migrated, migration_rate=rate)


async def _get_bonding_curves_via_program_accounts(
    creator: str, session: aiohttp.ClientSession
) -> dict[str, bool]:
    """
    Use getProgramAccounts with a memcmp filter at offset 81 (creator field) to
    find all bonding curves ever created by this wallet.

    Returns a dict mapping bonding_curve_pda_str -> is_migrated (complete=True
    OR account subsequently closed counts as migrated).
    """
    creator_b58 = str(Pubkey.from_string(creator))   # solders Pubkey.__str__ is base58
    payload = orjson.dumps({
        "jsonrpc": "2.0", "id": 1,
        "method": "getProgramAccounts",
        "params": [str(PUMP_PROGRAM_ID), {
            "encoding": "base64",
            "commitment": "confirmed",
            "filters": [
                {"memcmp": {"offset": BONDING_CURVE_CREATOR_OFFSET, "bytes": creator_b58}},
            ],
        }],
    })
    try:
        async with session.post(
            RPC_URL, data=payload,
            headers={"Content-Type": "application/json"},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as r:
            if r.status != 200:
                log.debug("getProgramAccounts HTTP %d for %s", r.status, creator[:8])
                return {}
            body = orjson.loads(await r.read())
            items = body.get("result", [])
            if not isinstance(items, list):
                log.debug("getProgramAccounts unexpected result for %s: %s", creator[:8], items)
                return {}
            result: dict[str, bool] = {}
            for item in items:
                pda_str  = item.get("pubkey", "")
                acct     = item.get("account", {})
                raw_data = acct.get("data", [None])[0]
                if not raw_data or not pda_str:
                    continue
                try:
                    bc_bytes = base64.b64decode(raw_data)
                    bc = BondingCurve.decode(bc_bytes)
                    result[pda_str] = bool(bc and bc.complete)
                except Exception:
                    result[pda_str] = False
            return result
    except Exception as exc:
        log.warning("getProgramAccounts failed for %s: %s", creator[:8], exc)
        return {}


async def _get_mints_via_logs(creator: str, session: aiohttp.ClientSession) -> list[str]:
    """
    Fallback: scan the creator's recent transaction logs for CreateEvent messages.
    Catches tokens from the last few minutes that might not yet be indexed.
    """
    sigs = await _get_creator_signatures(creator, session)
    if not sigs:
        return []
    return await _extract_created_mints(creator, sigs, session)


async def _get_creator_signatures(creator: str, session: aiohttp.ClientSession) -> list[str]:
    """Fetch the creator's own transaction history."""
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
    """
    Detect historical token creates by parsing CreateEvent log messages.
    Uses the same mechanism as the live monitor — robust to pump.fun program
    upgrades and wrapper programs that CPI into pump.fun.
    """
    mints: list[str] = []
    batch_size = 50

    for i in range(0, min(len(signatures), 500), batch_size):
        batch = signatures[i:i + batch_size]
        rpc_batch = [
            {
                "jsonrpc": "2.0", "id": idx,
                "method": "getTransaction",
                "params": [
                    sig,
                    {"encoding": "json", "commitment": "confirmed",
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
                    if not tx or tx.get("meta", {}).get("err"):
                        continue
                    for log_line in tx.get("meta", {}).get("logMessages", []):
                        if not log_line.startswith("Program data: "):
                            continue
                        try:
                            data = base64.b64decode(log_line[len("Program data: "):])
                            if len(data) < 8 or data[:8] != CREATE_EVENT_DISCRIMINATOR:
                                continue
                            event = CreateEvent.decode(data)
                            if event and str(event.creator) == creator:
                                mints.append(str(event.mint))
                        except Exception:
                            continue
        except Exception as exc:
            log.debug("batch getTransaction error: %s", exc)

    return mints


async def _count_migrations_batch(mints: list[str], session: aiohttp.ClientSession) -> int:
    """
    Check migration status for all mints in one getMultipleAccounts call per 100.
    complete=True or account closed both count as migrated.
    """
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
                data     = orjson.loads(await r.read())
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
