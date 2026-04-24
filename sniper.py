"""
Solana pump.fun memecoin sniper.

Filters new tokens by:
  - Developer migration rate  >= MIN_MIGRATION_RATE
  - Developer tokens launched >= MIN_TOKENS_LAUNCHED
  - Token has social media link (when REQUIRE_SOCIAL=true)

Submits buy transactions via Jito bundle (+ RPC fallback) for fastest execution.

Usage:
    cp .env.example .env   # fill in your keys and settings
    pip install -r requirements.txt
    python sniper.py
"""
import asyncio
import logging
import sys
from typing import Optional

import aiohttp
from solders.keypair import Keypair
import base58

import config
from monitor import stream_create_events
from analyzer import get_dev_stats, has_social_media
from trader import snipe, fetch_bonding_curve
from pumpfun import CreateEvent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("sniper")


def load_keypair() -> Keypair:
    raw = base58.b58decode(config.PRIVATE_KEY)
    return Keypair.from_bytes(raw)


async def evaluate_and_snipe(
    event: CreateEvent,
    keypair: Keypair,
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
) -> None:
    async with semaphore:
        creator_str = str(event.creator)
        mint_str = str(event.mint)

        log.info(
            "New token: %s (%s) | creator=%s",
            event.name, event.symbol, creator_str[:8] + "…",
        )

        # Run dev-stats and social-media checks concurrently
        dev_task = asyncio.create_task(get_dev_stats(creator_str, session))
        social_task = asyncio.create_task(
            has_social_media(event.uri, session) if config.REQUIRE_SOCIAL else _always_true()
        )

        dev_stats, social_ok = await asyncio.gather(dev_task, social_task)

        # --- Filter: developer history ---
        if dev_stats.tokens_launched < config.MIN_TOKENS_LAUNCHED:
            log.info(
                "  SKIP %s: dev only launched %d token(s) (need %d)",
                mint_str[:8], dev_stats.tokens_launched, config.MIN_TOKENS_LAUNCHED,
            )
            return

        if dev_stats.migration_rate < config.MIN_MIGRATION_RATE:
            log.info(
                "  SKIP %s: dev migration rate %.1f%% (need %.1f%%)",
                mint_str[:8],
                dev_stats.migration_rate * 100,
                config.MIN_MIGRATION_RATE * 100,
            )
            return

        # --- Filter: social media ---
        if config.REQUIRE_SOCIAL and not social_ok:
            log.info("  SKIP %s: no social media link in metadata", mint_str[:8])
            return

        log.info(
            "  MATCH %s | dev_rate=%.1f%% tokens_launched=%d social=%s | SNIPING...",
            mint_str[:8],
            dev_stats.migration_rate * 100,
            dev_stats.tokens_launched,
            social_ok,
        )

        # Fetch bonding curve for price calculation (best-effort, don't block)
        bc = await fetch_bonding_curve(event.mint, session)

        sig = await snipe(
            keypair=keypair,
            mint=event.mint,
            bonding_curve=event.bonding_curve,
            bc_data=bc,
            session=session,
        )

        if sig:
            log.info("  BUY submitted: %s | sig=%s", mint_str[:8], sig)
        else:
            log.warning("  BUY FAILED for %s", mint_str[:8])


async def _always_true() -> bool:
    return True


async def main() -> None:
    log.info("=== pump.fun memecoin sniper starting ===")
    log.info(
        "Filters: min_migration=%.0f%% | min_tokens=%d | require_social=%s",
        config.MIN_MIGRATION_RATE * 100,
        config.MIN_TOKENS_LAUNCHED,
        config.REQUIRE_SOCIAL,
    )
    log.info(
        "Buy: %.4f SOL | slippage=%.0f%% | priority=%d µL | jito=%s",
        config.BUY_AMOUNT_SOL,
        config.SLIPPAGE * 100,
        config.PRIORITY_FEE_MICROLAMPORTS,
        config.USE_JITO,
    )

    keypair = load_keypair()
    log.info("Wallet: %s", str(keypair.pubkey()))

    semaphore = asyncio.Semaphore(config.MAX_CONCURRENT_SNIPES)
    pending: set[asyncio.Task] = set()

    connector = aiohttp.TCPConnector(limit=50, ttl_dns_cache=300)
    async with aiohttp.ClientSession(connector=connector) as session:
        async for event in stream_create_events():
            task = asyncio.create_task(
                evaluate_and_snipe(event, keypair, session, semaphore)
            )
            pending.add(task)
            task.add_done_callback(pending.discard)

            # Prune completed tasks to avoid memory growth
            pending = {t for t in pending if not t.done()}


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Sniper stopped by user")
