"""
Paper trading sniper — identical filter pipeline to sniper.py, no real transactions.
All three async tasks (dev stats, social check, bonding curve fetch) run in parallel.
Telegram notifications on every buy, sell, and periodic summary.
"""
import asyncio
import logging
import os
import sys

os.environ.setdefault("PRIVATE_KEY", "paper_trading_no_real_key")

import aiohttp
import winloop

import config
from monitor import stream_create_events
from analyzer import get_dev_stats, has_social_media
from trader import fetch_bonding_curve, BLOCKHASH_CACHE
from pumpfun import CreateEvent
from paper_trader import PaperTrader
from notifier import notify, fmt_paper_buy, fmt_paper_sell

PAPER_STARTING_BALANCE_SOL = float(os.getenv("PAPER_STARTING_BALANCE_SOL", "10.0"))
SUMMARY_INTERVAL_SECONDS   = int(os.getenv("SUMMARY_INTERVAL_SECONDS",     "60"))

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
        mint_str    = str(event.mint)

        log.info("New token: %-12s %-8s | creator=%s",
                 event.name[:12], event.symbol, creator_str[:8] + "…")

        # Parallel: dev stats + social check + bonding curve fetch
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

        mc = bc.market_cap_sol() if bc else 0.0
        if bc:
            if config.MIN_MARKET_CAP_SOL > 0 and mc < config.MIN_MARKET_CAP_SOL:
                log.info("  SKIP %s: mcap %.2f < min %.2f SOL", mint_str[:8], mc, config.MIN_MARKET_CAP_SOL)
                return
            if config.MAX_MARKET_CAP_SOL > 0 and mc > config.MAX_MARKET_CAP_SOL:
                log.info("  SKIP %s: mcap %.2f > max %.2f SOL", mint_str[:8], mc, config.MAX_MARKET_CAP_SOL)
                return

        log.info("  MATCH %s | rate=%.1f%% tokens=%d social=%s | PAPER BUYING",
                 mint_str[:8], dev_stats.migration_rate * 100, dev_stats.tokens_launched, social_ok)

        pos = trader.buy(event, bc)
        if pos:
            notify(session, fmt_paper_buy(
                name=event.name, symbol=event.symbol, mint=mint_str,
                dev_rate=dev_stats.migration_rate * 100,
                dev_tokens=dev_stats.tokens_launched,
                sol_spent=pos.sol_spent, token_amount=pos.token_amount,
                entry_price=pos.entry_price_sol, market_cap=mc,
                balance=trader.balance_sol,
            ))


async def _position_updater(trader: PaperTrader, session: aiohttp.ClientSession) -> None:
    while True:
        await asyncio.sleep(SUMMARY_INTERVAL_SECONDS)

        closed = await trader.update_positions(session)
        for pos in closed:
            notify(session, fmt_paper_sell(
                name=pos.name, symbol=pos.symbol, mint=pos.mint,
                pnl_sol=pos.pnl_sol, pnl_pct=pos.pnl_pct,
                exit_value=pos.exit_value_sol, reason=pos.close_reason,
                balance=trader.balance_sol,
            ))

        log.info("\n%s", trader.summary())
        trader.save_log()


async def _true() -> bool:
    return True


async def main() -> None:
    log.info("=== pump.fun PAPER SNIPER starting ===")
    log.info("Starting balance : %.2f SOL | buy=%.4f SOL",
             PAPER_STARTING_BALANCE_SOL, config.BUY_AMOUNT_SOL)
    log.info("Filters: migration>=%.0f%% | tokens>=%d | social=%s | mcap=[%.1f,%s] SOL",
             config.MIN_MIGRATION_RATE * 100, config.MIN_TOKENS_LAUNCHED, config.REQUIRE_SOCIAL,
             config.MIN_MARKET_CAP_SOL,
             f"{config.MAX_MARKET_CAP_SOL:.1f}" if config.MAX_MARKET_CAP_SOL > 0 else "∞")
    log.info("Summary interval : %ds | log: paper_trades.json", SUMMARY_INTERVAL_SECONDS)

    trader    = PaperTrader(PAPER_STARTING_BALANCE_SOL, config.BUY_AMOUNT_SOL, config.SLIPPAGE,
                            take_profit_multiple=config.TAKE_PROFIT_MULTIPLE)
    semaphore = asyncio.Semaphore(config.MAX_CONCURRENT_SNIPES)
    pending: set[asyncio.Task] = set()

    connector = aiohttp.TCPConnector(limit=100, ttl_dns_cache=300, enable_cleanup_closed=True)
    async with aiohttp.ClientSession(connector=connector) as session:
        BLOCKHASH_CACHE.start(session)
        asyncio.create_task(_position_updater(trader, session))

        async for event in stream_create_events():
            task = asyncio.create_task(evaluate_and_paper_buy(event, trader, session, semaphore))
            pending.add(task)
            task.add_done_callback(pending.discard)
            pending = {t for t in pending if not t.done()}


if __name__ == "__main__":
    try:
        winloop.run(main())
    except KeyboardInterrupt:
        log.info("Paper sniper stopped")
