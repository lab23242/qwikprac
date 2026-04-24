"""
Paper trading sniper — identical filters to sniper.py, no real transactions.
Tracks virtual P&L against live bonding curve prices.
Prints a portfolio summary every SUMMARY_INTERVAL_SECONDS seconds.
Trade history is written to paper_trades.json on every update.

Usage:
    python paper_sniper.py
"""
import asyncio
import logging
import os
import sys

# Set a dummy private key before config imports so PRIVATE_KEY check passes.
# Paper trading never signs real transactions.
os.environ.setdefault("PRIVATE_KEY", "paper_trading_no_real_key")

import aiohttp

import config
from monitor import stream_create_events
from analyzer import get_dev_stats, has_social_media
from trader import fetch_bonding_curve
from pumpfun import CreateEvent
from paper_trader import PaperTrader

PAPER_STARTING_BALANCE_SOL = float(os.getenv("PAPER_STARTING_BALANCE_SOL", "10.0"))
SUMMARY_INTERVAL_SECONDS = int(os.getenv("SUMMARY_INTERVAL_SECONDS", "60"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("paper_sniper")


async def evaluate_and_paper_buy(
    event: CreateEvent,
    trader: PaperTrader,
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
) -> None:
    async with semaphore:
        creator_str = str(event.creator)
        mint_str = str(event.mint)

        log.info(
            "New token: %-12s %-6s | creator=%s",
            event.name[:12], event.symbol, creator_str[:8] + "…",
        )

        dev_task = asyncio.create_task(get_dev_stats(creator_str, session))
        social_task = asyncio.create_task(
            has_social_media(event.uri, session) if config.REQUIRE_SOCIAL else _true()
        )
        dev_stats, social_ok = await asyncio.gather(dev_task, social_task)

        if dev_stats.tokens_launched < config.MIN_TOKENS_LAUNCHED:
            log.info(
                "  SKIP %s: dev launched %d (need %d)",
                mint_str[:8], dev_stats.tokens_launched, config.MIN_TOKENS_LAUNCHED,
            )
            return

        if dev_stats.migration_rate < config.MIN_MIGRATION_RATE:
            log.info(
                "  SKIP %s: migration rate %.1f%% (need %.1f%%)",
                mint_str[:8], dev_stats.migration_rate * 100, config.MIN_MIGRATION_RATE * 100,
            )
            return

        if config.REQUIRE_SOCIAL and not social_ok:
            log.info("  SKIP %s: no social media link", mint_str[:8])
            return

        bc = await fetch_bonding_curve(event.mint, session)

        if bc:
            mc = bc.market_cap_sol()
            if config.MIN_MARKET_CAP_SOL > 0 and mc < config.MIN_MARKET_CAP_SOL:
                log.info(
                    "  SKIP %s: mcap %.2f SOL < min %.2f SOL",
                    mint_str[:8], mc, config.MIN_MARKET_CAP_SOL,
                )
                return
            if config.MAX_MARKET_CAP_SOL > 0 and mc > config.MAX_MARKET_CAP_SOL:
                log.info(
                    "  SKIP %s: mcap %.2f SOL > max %.2f SOL",
                    mint_str[:8], mc, config.MAX_MARKET_CAP_SOL,
                )
                return

        log.info(
            "  MATCH %s | rate=%.1f%% tokens=%d social=%s | PAPER BUYING",
            mint_str[:8], dev_stats.migration_rate * 100,
            dev_stats.tokens_launched, social_ok,
        )
        trader.buy(event, bc)


async def _position_updater(trader: PaperTrader, session: aiohttp.ClientSession) -> None:
    """Background task: refresh prices, print summary, persist log."""
    while True:
        await asyncio.sleep(SUMMARY_INTERVAL_SECONDS)
        if trader.positions:
            await trader.update_positions(session)
        log.info("\n%s", trader.summary())
        trader.save_log()


async def _true() -> bool:
    return True


async def main() -> None:
    log.info("=== pump.fun PAPER SNIPER starting ===")
    log.info("Starting balance : %.2f SOL", PAPER_STARTING_BALANCE_SOL)
    log.info("Buy per snipe    : %.4f SOL", config.BUY_AMOUNT_SOL)
    log.info(
        "Filters          : migration>=%.0f%% | tokens>=%d | social=%s | mcap=[%.1f,%s] SOL",
        config.MIN_MIGRATION_RATE * 100,
        config.MIN_TOKENS_LAUNCHED,
        config.REQUIRE_SOCIAL,
        config.MIN_MARKET_CAP_SOL,
        f"{config.MAX_MARKET_CAP_SOL:.1f}" if config.MAX_MARKET_CAP_SOL > 0 else "∞",
    )
    log.info("Summary interval : %ds  |  log file: paper_trades.json", SUMMARY_INTERVAL_SECONDS)

    trader = PaperTrader(
        starting_balance_sol=PAPER_STARTING_BALANCE_SOL,
        buy_amount_sol=config.BUY_AMOUNT_SOL,
        slippage=config.SLIPPAGE,
    )

    semaphore = asyncio.Semaphore(config.MAX_CONCURRENT_SNIPES)
    pending: set[asyncio.Task] = set()

    connector = aiohttp.TCPConnector(limit=50, ttl_dns_cache=300)
    async with aiohttp.ClientSession(connector=connector) as session:
        asyncio.create_task(_position_updater(trader, session))

        async for event in stream_create_events():
            task = asyncio.create_task(
                evaluate_and_paper_buy(event, trader, session, semaphore)
            )
            pending.add(task)
            task.add_done_callback(pending.discard)
            pending = {t for t in pending if not t.done()}


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Paper sniper stopped by user")
