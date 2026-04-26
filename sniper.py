"""
Live sniper — real transactions via Jito bundle + RPC fallback.
All three async tasks (dev stats, social check, bonding curve fetch) run in
parallel so the buy fires the instant all filters pass.
"""
import asyncio
import logging
import sys

import aiohttp
from solders.keypair import Keypair
import base58
import winloop

import config
from monitor import stream_create_events
from analyzer import get_dev_stats, has_social_media
from trader import snipe, fetch_bonding_curve, BLOCKHASH_CACHE
from pumpfun import CreateEvent
from notifier import notify, fmt_snipe_buy

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("sniper")


def load_keypair() -> Keypair:
    return Keypair.from_bytes(base58.b58decode(config.PRIVATE_KEY))


async def evaluate_and_snipe(
    event: CreateEvent,
    keypair: Keypair,
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
) -> None:
    async with semaphore:
        creator_str = str(event.creator)
        mint_str    = str(event.mint)

        log.info("New token: %-12s %-8s | creator=%s", event.name[:12], event.symbol, creator_str[:8] + "…")

        # All three run concurrently — bonding curve fetch no longer waits behind filters
        dev_task    = asyncio.create_task(get_dev_stats(creator_str, session))
        social_task = asyncio.create_task(
            has_social_media(event.uri, session) if config.REQUIRE_SOCIAL else _true()
        )
        bc_task     = asyncio.create_task(fetch_bonding_curve(event.mint, session))

        dev_stats, social_ok, bc = await asyncio.gather(dev_task, social_task, bc_task)

        if dev_stats.tokens_launched < config.MIN_TOKENS_LAUNCHED:
            log.info("  SKIP %s: dev launched %d (need %d)",
                     mint_str[:8], dev_stats.tokens_launched, config.MIN_TOKENS_LAUNCHED)
            return

        if dev_stats.migration_rate < config.MIN_MIGRATION_RATE:
            log.info("  SKIP %s: migration %.1f%% (need %.1f%%)",
                     mint_str[:8], dev_stats.migration_rate * 100, config.MIN_MIGRATION_RATE * 100)
            return

        if config.REQUIRE_SOCIAL and not social_ok:
            log.info("  SKIP %s: no social media", mint_str[:8])
            return

        if bc:
            mc = bc.market_cap_sol()
            if config.MIN_MARKET_CAP_SOL > 0 and mc < config.MIN_MARKET_CAP_SOL:
                log.info("  SKIP %s: mcap %.2f < min %.2f SOL", mint_str[:8], mc, config.MIN_MARKET_CAP_SOL)
                return
            if config.MAX_MARKET_CAP_SOL > 0 and mc > config.MAX_MARKET_CAP_SOL:
                log.info("  SKIP %s: mcap %.2f > max %.2f SOL", mint_str[:8], mc, config.MAX_MARKET_CAP_SOL)
                return

        log.info("  MATCH %s | rate=%.1f%% tokens=%d social=%s | SNIPING",
                 mint_str[:8], dev_stats.migration_rate * 100, dev_stats.tokens_launched, social_ok)

        sig = await snipe(
            keypair=keypair, mint=event.mint,
            bonding_curve=event.bonding_curve, bc_data=bc, session=session,
            creator=event.creator,
        )

        if sig:
            log.info("  BUY submitted: %s", sig)
            notify(session, fmt_snipe_buy(
                name=event.name, symbol=event.symbol, mint=mint_str,
                dev_rate=dev_stats.migration_rate * 100,
                dev_tokens=dev_stats.tokens_launched,
                sol_spent=config.BUY_AMOUNT_SOL, sig=sig,
            ))
        else:
            log.warning("  BUY FAILED for %s", mint_str[:8])


async def _true() -> bool:
    return True


async def main() -> None:
    log.info("=== pump.fun live sniper starting ===")
    log.info("Filters: migration>=%.0f%% | tokens>=%d | social=%s | mcap=[%.1f,%s] SOL",
             config.MIN_MIGRATION_RATE * 100, config.MIN_TOKENS_LAUNCHED, config.REQUIRE_SOCIAL,
             config.MIN_MARKET_CAP_SOL,
             f"{config.MAX_MARKET_CAP_SOL:.1f}" if config.MAX_MARKET_CAP_SOL > 0 else "∞")
    log.info("Buy: %.4f SOL | slippage=%.0f%% | priority=%d µL | jito=%s",
             config.BUY_AMOUNT_SOL, config.SLIPPAGE * 100,
             config.PRIORITY_FEE_MICROLAMPORTS, config.USE_JITO)

    keypair = load_keypair()
    log.info("Wallet: %s", keypair.pubkey())

    semaphore = asyncio.Semaphore(config.MAX_CONCURRENT_SNIPES)
    pending: set[asyncio.Task] = set()

    connector = aiohttp.TCPConnector(limit=100, ttl_dns_cache=300, enable_cleanup_closed=True)
    async with aiohttp.ClientSession(connector=connector) as session:
        BLOCKHASH_CACHE.start(session)

        async for event in stream_create_events():
            task = asyncio.create_task(evaluate_and_snipe(event, keypair, session, semaphore))
            pending.add(task)
            task.add_done_callback(pending.discard)
            pending = {t for t in pending if not t.done()}


if __name__ == "__main__":
    try:
        winloop.run(main())
    except KeyboardInterrupt:
        log.info("Sniper stopped")
