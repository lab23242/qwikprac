"""
Diagnostic script — tests every step of the dev-history detection pipeline.
Run:  python test_dev_history.py [creator_wallet]
If no wallet is given, it auto-discovers a recent pump.fun creator from program logs.
"""
import asyncio
import base64
import hashlib
import os
import sys

import aiohttp
import orjson

os.environ.setdefault("PRIVATE_KEY", "test_no_real_key")

from dotenv import load_dotenv
load_dotenv()

RPC_URL          = os.getenv("RPC_URL", "https://api.mainnet-beta.solana.com")
PUMP_PROGRAM_ID  = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"

from pumpfun import CreateEvent  # noqa — needs env/dotenv first

CREATE_DISCRIMINATOR       = hashlib.sha256(b"global:create").digest()[:8]
BUY_DISCRIMINATOR          = hashlib.sha256(b"global:buy").digest()[:8]
SELL_DISCRIMINATOR         = hashlib.sha256(b"global:sell").digest()[:8]
CREATE_EVENT_DISCRIMINATOR = hashlib.sha256(b"event:CreateEvent").digest()[:8]

KNOWN_DISCS = {
    CREATE_DISCRIMINATOR.hex():       "create",
    BUY_DISCRIMINATOR.hex():          "buy",
    SELL_DISCRIMINATOR.hex():         "sell",
    CREATE_EVENT_DISCRIMINATOR.hex(): "CreateEvent",
}

HEADERS = {"Content-Type": "application/json"}


# ---------------------------------------------------------------------------
# Step 0: show discriminator values
# ---------------------------------------------------------------------------

def print_discriminators():
    print("=== Anchor discriminators ===")
    for hex_val, name in KNOWN_DISCS.items():
        print(f"  {name:20s}  {hex_val}")
    print()


# ---------------------------------------------------------------------------
# Step 1: find a recent pump.fun create tx and inspect its raw instruction
# ---------------------------------------------------------------------------

async def find_recent_creator(session: aiohttp.ClientSession) -> str | None:
    """Return the creator pubkey from the most recent pump.fun create on-chain."""
    print("=== Step 1: find a recent pump.fun create transaction ===")

    # grab recent program sigs
    payload = orjson.dumps({
        "jsonrpc": "2.0", "id": 1,
        "method": "getSignaturesForAddress",
        "params": [PUMP_PROGRAM_ID, {"limit": 20, "commitment": "confirmed"}],
    })
    async with session.post(RPC_URL, data=payload, headers=HEADERS,
                            timeout=aiohttp.ClientTimeout(total=10)) as r:
        body = await r.read()
        if r.status != 200:
            print(f"  HTTP {r.status}: {body[:200]}")
            return None
        try:
            data = orjson.loads(body)
        except Exception:
            print(f"  Non-JSON response ({r.status}): {body[:200]}")
            return None
        if "error" in data:
            print(f"  RPC error: {data['error']}")
            return None
    sigs = [s["signature"] for s in data.get("result", []) if not s.get("err")]
    print(f"  Got {len(sigs)} recent pump.fun signatures")

    if not sigs:
        print("  Public RPC likely rate-limiting program-level queries.")
        print("  Re-run with: python test_dev_history.py <CREATOR_WALLET>")
        return None

    # fetch them in a batch (jsonParsed)
    rpc_batch = [
        {"jsonrpc": "2.0", "id": i, "method": "getTransaction",
         "params": [sig, {"encoding": "jsonParsed", "commitment": "confirmed",
                          "maxSupportedTransactionVersion": 0}]}
        for i, sig in enumerate(sigs)
    ]
    async with session.post(RPC_URL, data=orjson.dumps(rpc_batch), headers=HEADERS,
                            timeout=aiohttp.ClientTimeout(total=30)) as r:
        body = await r.read()
        if r.status != 200:
            print(f"  Batch fetch HTTP {r.status}: {body[:200]}")
            return None
        try:
            results = orjson.loads(body)
        except Exception:
            print(f"  Non-JSON batch response: {body[:200]}")
            return None

    creates_found = 0
    for item in results:
        tx = item.get("result")
        if not tx or tx.get("meta", {}).get("err"):
            continue
        for log_line in tx.get("meta", {}).get("logMessages", []):
            if not log_line.startswith("Program data: "):
                continue
            try:
                event = CreateEvent.decode(
                    base64.b64decode(log_line[len("Program data: "):])
                )
                if event:
                    creates_found += 1
                    creator = str(event.creator)
                    mint    = str(event.mint)
                    print(f"  --> CreateEvent via log: creator={creator[:8]}… mint={mint[:8]}…")
                    return creator
            except Exception:
                continue

    if creates_found == 0:
        print("  No CreateEvent found in recent transactions — pump.fun may be quiet or RPC lagging")
    return None


# ---------------------------------------------------------------------------
# Step 2: run _get_creator_signatures against that wallet
# ---------------------------------------------------------------------------

async def test_signatures(creator: str, session: aiohttp.ClientSession) -> list[str]:
    print(f"\n=== Step 2: getSignaturesForAddress for {creator[:16]}… ===")
    payload = orjson.dumps({
        "jsonrpc": "2.0", "id": 1,
        "method": "getSignaturesForAddress",
        "params": [creator, {"limit": 1000, "commitment": "confirmed"}],
    })
    async with session.post(RPC_URL, data=payload, headers=HEADERS,
                            timeout=aiohttp.ClientTimeout(total=10)) as r:
        body = await r.read()
        data = orjson.loads(body)

    raw      = data.get("result", [])
    ok_sigs  = [s["signature"] for s in raw if not s.get("err")]
    err_sigs = [s for s in raw if s.get("err")]
    print(f"  Total sigs: {len(raw)} | ok: {len(ok_sigs)} | errored: {len(err_sigs)}")
    return ok_sigs


# ---------------------------------------------------------------------------
# Step 3: fetch & inspect the transactions; count create ix matches
# ---------------------------------------------------------------------------

async def test_extract_mints(creator: str, sigs: list[str],
                             session: aiohttp.ClientSession) -> list[str]:
    print(f"\n=== Step 3: batch getTransaction ({min(len(sigs),50)} of {len(sigs)} sigs) ===")
    print("  Strategy: decode CreateEvent log messages (same as live monitor)")
    batch = sigs[:50]
    rpc_batch = [
        {"jsonrpc": "2.0", "id": idx, "method": "getTransaction",
         "params": [sig, {"encoding": "json", "commitment": "confirmed",
                          "maxSupportedTransactionVersion": 0}]}
        for idx, sig in enumerate(batch)
    ]
    async with session.post(RPC_URL, data=orjson.dumps(rpc_batch), headers=HEADERS,
                            timeout=aiohttp.ClientTimeout(total=20)) as r:
        results = orjson.loads(await r.read())

    mints        = []
    fetched      = 0
    log_lines    = 0
    event_hits   = 0

    for item in results:
        tx = item.get("result")
        if not tx or tx.get("meta", {}).get("err"):
            continue
        fetched += 1
        for log_line in tx.get("meta", {}).get("logMessages", []):
            if not log_line.startswith("Program data: "):
                continue
            log_lines += 1
            try:
                data = base64.b64decode(log_line[len("Program data: "):])
                if len(data) < 8 or data[:8] != CREATE_EVENT_DISCRIMINATOR:
                    continue
                event = CreateEvent.decode(data)
                if event:
                    event_hits += 1
                    match = str(event.creator) == creator
                    print(f"  CreateEvent: mint={str(event.mint)[:8]}… "
                          f"creator={str(event.creator)[:8]}… matches={match}")
                    if match:
                        mints.append(str(event.mint))
            except Exception:
                continue

    print(f"  Fetched {fetched}/{len(batch)} txns | 'Program data:' lines: {log_lines} "
          f"| CreateEvents: {event_hits} | matched mints: {len(mints)}")
    return mints


# ---------------------------------------------------------------------------
# Step 4: verify migration status of found mints
# ---------------------------------------------------------------------------

async def test_migrations(mints: list[str], session: aiohttp.ClientSession) -> None:
    if not mints:
        print("\n=== Step 4: no mints to check ===")
        return
    print(f"\n=== Step 4: migration check for {len(mints)} mints ===")

    from solders.pubkey import Pubkey
    from pumpfun import get_bonding_curve_pda, BondingCurve

    pdas = [str(get_bonding_curve_pda(Pubkey.from_string(m))) for m in mints]
    payload = orjson.dumps({
        "jsonrpc": "2.0", "id": 1,
        "method": "getMultipleAccounts",
        "params": [pdas, {"encoding": "base64", "commitment": "confirmed"}],
    })
    async with session.post(RPC_URL, data=payload, headers=HEADERS,
                            timeout=aiohttp.ClientTimeout(total=15)) as r:
        data     = orjson.loads(await r.read())
        accounts = data["result"]["value"]

    migrated = 0
    for mint, acct in zip(mints, accounts):
        if acct is None:
            print(f"  {mint[:8]}… CLOSED (migrated)")
            migrated += 1
        else:
            try:
                bc = BondingCurve.decode(base64.b64decode(acct["data"][0]))
                status = "migrated(complete)" if bc and bc.complete else "active"
                if bc and bc.complete:
                    migrated += 1
            except Exception:
                status = "decode-error"
            print(f"  {mint[:8]}… {status}")

    print(f"  Migrated: {migrated}/{len(mints)}  rate={migrated/len(mints)*100:.0f}%")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    print_discriminators()

    creator = sys.argv[1] if len(sys.argv) > 1 else None

    connector = aiohttp.TCPConnector(limit=20)
    async with aiohttp.ClientSession(connector=connector) as session:
        if not creator:
            creator = await find_recent_creator(session)
        else:
            print(f"Using provided creator: {creator}")

        if not creator:
            print("\nCould not find a creator — pass one as an argument:")
            print("  python test_dev_history.py <CREATOR_WALLET_ADDRESS>")
            return

        sigs  = await test_signatures(creator, session)
        mints = await test_extract_mints(creator, sigs, session)
        await test_migrations(mints, session)

        print("\n=== Summary ===")
        print(f"  Creator  : {creator}")
        print(f"  Sigs     : {len(sigs)}")
        print(f"  Mints    : {len(mints)}")


if __name__ == "__main__":
    asyncio.run(main())
