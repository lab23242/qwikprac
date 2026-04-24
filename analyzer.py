"""
Developer analysis: computes migration rate and past token count for a creator,
and checks token metadata for social media links.
Both results are cached with a short TTL to avoid redundant RPC calls.
"""
import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import aiohttp

from config import RPC_URL
from pumpfun import PUMP_PROGRAM_ID, get_bonding_curve_pda, BondingCurve

log = logging.getLogger(__name__)

_dev_cache: dict[str, tuple[float, "DevStats"]] = {}  # pubkey -> (timestamp, stats)
_DEV_CACHE_TTL = 300  # seconds

_meta_cache: dict[str, tuple[float, bool]] = {}  # uri -> (timestamp, has_social)
_META_CACHE_TTL = 60


@dataclass
class DevStats:
    tokens_launched: int = 0
    tokens_migrated: int = 0
    migration_rate: float = 0.0


async def get_dev_stats(creator: str, session: aiohttp.ClientSession) -> DevStats:
    """Return cached-or-fresh stats for a developer wallet."""
    now = time.monotonic()
    cached = _dev_cache.get(creator)
    if cached and (now - cached[0]) < _DEV_CACHE_TTL:
        return cached[1]

    stats = await _fetch_dev_stats(creator, session)
    _dev_cache[creator] = (now, stats)
    return stats


async def _fetch_dev_stats(creator: str, session: aiohttp.ClientSession) -> DevStats:
    """
    Fetch all signatures where the creator used the pump.fun program,
    then count migrations by checking bonding curve `complete` flag.
    """
    sigs = await _get_signatures(creator, session)
    if not sigs:
        return DevStats()

    # Collect mints from create instructions in those transactions
    mints = await _get_created_mints(creator, sigs, session)
    if not mints:
        return DevStats()

    # Check migration status for all mints concurrently
    migrated = await _count_migrations(mints, session)
    rate = migrated / len(mints) if mints else 0.0
    return DevStats(tokens_launched=len(mints), tokens_migrated=migrated, migration_rate=rate)


async def _get_signatures(creator: str, session: aiohttp.ClientSession) -> list[str]:
    """Fetch up to 1000 signatures for the creator <> pump.fun interaction."""
    payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "getSignaturesForAddress",
        "params": [
            str(PUMP_PROGRAM_ID),
            {"limit": 1000, "commitment": "confirmed"},
        ],
    }
    try:
        async with session.post(RPC_URL, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as r:
            data = await r.json()
            sigs = [s["signature"] for s in data.get("result", [])]
            return sigs
    except Exception as exc:
        log.warning("getSignaturesForAddress failed for %s: %s", creator, exc)
        return []


async def _get_created_mints(
    creator: str, signatures: list[str], session: aiohttp.ClientSession
) -> list[str]:
    """
    Fetch transactions in batches and extract mint addresses from
    pump.fun create instructions authored by `creator`.
    """
    mints: list[str] = []
    batch_size = 50

    for i in range(0, min(len(signatures), 500), batch_size):
        batch = signatures[i:i + batch_size]
        tasks = [_parse_create_tx(sig, creator, session) for sig in batch]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, str):
                mints.append(r)

    return mints


async def _parse_create_tx(
    sig: str, creator: str, session: aiohttp.ClientSession
) -> Optional[str]:
    """Return the mint address if this tx is a pump.fun create by `creator`."""
    payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "getTransaction",
        "params": [sig, {"encoding": "jsonParsed", "commitment": "confirmed", "maxSupportedTransactionVersion": 0}],
    }
    try:
        async with session.post(RPC_URL, json=payload, timeout=aiohttp.ClientTimeout(total=8)) as r:
            data = await r.json()
            tx = data.get("result")
            if not tx:
                return None
            return _extract_mint_from_tx(tx, creator)
    except Exception:
        return None


def _extract_mint_from_tx(tx: dict, creator: str) -> Optional[str]:
    """
    Heuristic: find a create instruction to pump.fun from `creator`.
    The mint is the first new account key not in common programs.
    We look for the account that becomes a new Token Mint in the tx.
    """
    try:
        meta = tx.get("meta", {})
        if meta.get("err"):
            return None
        msg = tx["transaction"]["message"]
        # Fee payer must be creator
        account_keys = msg["accountKeys"]
        if account_keys[0]["pubkey"] != creator:
            return None
        # Look for a pump.fun instruction
        for ix in msg.get("instructions", []):
            if ix.get("programId") == str(PUMP_PROGRAM_ID):
                # The mint is typically the 3rd account in create (after global, payer)
                accs = ix.get("accounts", [])
                if len(accs) >= 3:
                    return accs[2]  # mint position in create instruction
        # Also check inner instructions
        for inner_group in meta.get("innerInstructions", []):
            for ix in inner_group.get("instructions", []):
                if ix.get("programId") == str(PUMP_PROGRAM_ID):
                    accs = ix.get("accounts", [])
                    if len(accs) >= 3:
                        return accs[2]
    except (KeyError, IndexError, TypeError):
        pass
    return None


async def _count_migrations(mints: list[str], session: aiohttp.ClientSession) -> int:
    """Check how many bonding curves have complete=True (migrated to Raydium)."""
    tasks = [_is_migrated(mint, session) for mint in mints]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    return sum(1 for r in results if r is True)


async def _is_migrated(mint_str: str, session: aiohttp.ClientSession) -> bool:
    try:
        from solders.pubkey import Pubkey
        mint = Pubkey.from_string(mint_str)
        bc_pda = get_bonding_curve_pda(mint)
        payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": "getAccountInfo",
            "params": [str(bc_pda), {"encoding": "base64", "commitment": "confirmed"}],
        }
        async with session.post(RPC_URL, json=payload, timeout=aiohttp.ClientTimeout(total=6)) as r:
            data = await r.json()
            result = data.get("result", {})
            if result is None or result.get("value") is None:
                # Account closed = migrated
                return True
            raw = result["value"]["data"]
            if isinstance(raw, list):
                import base64 as _b64
                bc_data = _b64.b64decode(raw[0])
            else:
                return False
            bc = BondingCurve.decode(bc_data)
            return bc.complete if bc else False
    except Exception:
        return False


async def has_social_media(uri: str, session: aiohttp.ClientSession) -> bool:
    """Fetch token metadata URI and check for twitter/telegram/website fields."""
    if not uri:
        return False

    now = time.monotonic()
    cached = _meta_cache.get(uri)
    if cached and (now - cached[0]) < _META_CACHE_TTL:
        return cached[1]

    result = await _fetch_social(uri, session)
    _meta_cache[uri] = (now, result)
    return result


async def _fetch_social(uri: str, session: aiohttp.ClientSession) -> bool:
    # Resolve IPFS URIs via public gateway
    url = _resolve_uri(uri)
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as r:
            if r.status != 200:
                return False
            meta = await r.json(content_type=None)
            social_fields = ("twitter", "telegram", "website", "discord", "x")
            return any(meta.get(f) for f in social_fields)
    except Exception as exc:
        log.debug("metadata fetch failed for %s: %s", uri, exc)
        return False


def _resolve_uri(uri: str) -> str:
    if uri.startswith("ipfs://"):
        return "https://ipfs.io/ipfs/" + uri[7:]
    if uri.startswith("https://cf-ipfs.com/") or uri.startswith("https://ipfs.io/"):
        return uri
    return uri
