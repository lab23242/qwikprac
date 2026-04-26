"""
Telegram notification module.
Sends fire-and-forget messages to every configured TELEGRAM_CHAT_IDS.
Uses the Bot API directly over aiohttp — no heavy library dependency.
"""
import asyncio
import logging

import aiohttp

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_IDS

log = logging.getLogger(__name__)

_enabled = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_IDS)
_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"


def notify(session: aiohttp.ClientSession, text: str) -> None:
    """Schedule non-blocking Telegram messages (fire-and-forget)."""
    if not _enabled:
        return
    for chat_id in TELEGRAM_CHAT_IDS:
        asyncio.create_task(_send(session, chat_id, text))


async def _send(session: aiohttp.ClientSession, chat_id: str, text: str) -> None:
    try:
        async with session.post(
            _API,
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=aiohttp.ClientTimeout(total=6),
        ) as r:
            if r.status != 200:
                body = await r.text()
                log.debug("Telegram %s: HTTP %d — %s", chat_id, r.status, body[:120])
    except Exception as exc:
        log.debug("Telegram send error for %s: %s", chat_id, exc)


# ---------------------------------------------------------------------------
# Message formatters
# ---------------------------------------------------------------------------

def fmt_paper_buy(name: str, symbol: str, mint: str, dev_rate: float,
                  dev_tokens: int, sol_spent: float, token_amount: int,
                  entry_price: float, market_cap: float, balance: float) -> str:
    return (
        f"🎯 <b>PAPER BUY</b>\n"
        f"<b>Token:</b> {_esc(name)} (<code>{_esc(symbol)}</code>)\n"
        f"<b>Mint:</b> <code>{mint}</code>\n"
        f"<b>Dev:</b> migration {dev_rate:.0f}% | {dev_tokens} launched\n"
        f"<b>Spent:</b> {sol_spent:.4f} SOL | "
        f"<b>Tokens:</b> {token_amount / 1e6:,.2f}\n"
        f"<b>Market cap:</b> {market_cap:.1f} SOL\n"
        f"<b>Balance:</b> {balance:.4f} SOL"
    )


def fmt_paper_sell(name: str, symbol: str, mint: str, pnl_sol: float, pnl_pct: float,
                   exit_value: float, reason: str, balance: float) -> str:
    emoji = "🟢" if pnl_sol >= 0 else "🔴"
    sign  = "+" if pnl_sol >= 0 else ""
    return (
        f"{emoji} <b>PAPER SELL</b>\n"
        f"<b>Token:</b> {_esc(name)} (<code>{_esc(symbol)}</code>)\n"
        f"<b>Mint:</b> <code>{mint}</code>\n"
        f"<b>P&amp;L:</b> {sign}{pnl_sol:.4f} SOL ({sign}{pnl_pct:.1f}%)\n"
        f"<b>Received:</b> {exit_value:.4f} SOL\n"
        f"<b>Reason:</b> {reason}\n"
        f"<b>Balance:</b> {balance:.4f} SOL"
    )


def fmt_snipe_buy(name: str, symbol: str, mint: str, dev_rate: float,
                  dev_tokens: int, sol_spent: float, sig: str) -> str:
    return (
        f"⚡ <b>SNIPE BUY</b>\n"
        f"<b>Token:</b> {_esc(name)} (<code>{_esc(symbol)}</code>)\n"
        f"<b>Mint:</b> <code>{mint}</code>\n"
        f"<b>Dev:</b> migration {dev_rate:.0f}% | {dev_tokens} launched\n"
        f"<b>Spent:</b> {sol_spent:.4f} SOL\n"
        f"<b>Sig:</b> <code>{sig[:20]}…</code>"
    )


def fmt_summary(balance: float, open_count: int, unrealized: float,
                total_value: float, total_pnl: float, total_pnl_pct: float,
                closed: int, wins: int, losses: int) -> str:
    sign = "+" if total_pnl >= 0 else ""
    wr   = f"{wins/(closed)*100:.0f}%" if closed else "n/a"
    return (
        f"📊 <b>PORTFOLIO SUMMARY</b>\n"
        f"<b>Balance:</b> {balance:.4f} SOL\n"
        f"<b>Open:</b> {open_count}  (unrealized {unrealized:.4f} SOL)\n"
        f"<b>Total value:</b> {total_value:.4f} SOL\n"
        f"<b>P&amp;L:</b> {sign}{total_pnl:.4f} SOL ({sign}{total_pnl_pct:.1f}%)\n"
        f"<b>Closed:</b> {closed}  W:{wins} L:{losses} WR:{wr}"
    )


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
