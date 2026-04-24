"""
Paper trading engine.
Records virtual buys/sells, tracks P&L against live bonding curve prices,
and auto-closes positions when a token migrates (complete=True or account closed).
"""
import asyncio
import json
import logging
import time
from dataclasses import dataclass, asdict
from typing import Optional

import aiohttp

from pumpfun import BondingCurve, CreateEvent, get_bonding_curve_pda

log = logging.getLogger(__name__)


@dataclass
class Position:
    mint: str
    name: str
    symbol: str
    entry_price_sol: float      # SOL per raw token unit
    token_amount: int           # raw token units
    sol_spent: float
    entry_time: float
    current_price_sol: float = 0.0
    current_value_sol: float = 0.0
    pnl_sol: float = 0.0
    pnl_pct: float = 0.0
    migrated: bool = False
    closed: bool = False
    exit_price_sol: float = 0.0
    exit_time: float = 0.0
    exit_value_sol: float = 0.0


@dataclass
class Trade:
    mint: str
    name: str
    symbol: str
    side: str           # BUY | SELL
    price_sol: float
    token_amount: int
    sol_amount: float
    timestamp: float
    reason: str = ""


class PaperTrader:
    def __init__(self, starting_balance_sol: float, buy_amount_sol: float, slippage: float):
        self.balance_sol = starting_balance_sol
        self.starting_balance = starting_balance_sol
        self.buy_amount_sol = buy_amount_sol
        self.slippage = slippage
        self.positions: dict[str, Position] = {}
        self.closed_positions: list[Position] = []
        self.trades: list[Trade] = []

    def buy(self, event: CreateEvent, bc: Optional[BondingCurve]) -> bool:
        mint_str = str(event.mint)
        if mint_str in self.positions:
            return False
        if self.balance_sol < self.buy_amount_sol:
            log.warning("[PAPER] Insufficient balance: %.4f SOL (need %.4f)", self.balance_sol, self.buy_amount_sol)
            return False

        sol_in = self.buy_amount_sol
        if bc:
            token_amount = bc.tokens_for_sol(sol_in)
            entry_price = bc.token_price_in_sol()
        else:
            # Fallback: use pump.fun launch-state price estimate
            entry_price = 30e9 / 1_073_000_000_000_000 / 1e9
            token_amount = int(sol_in / entry_price) if entry_price > 0 else 0

        self.balance_sol -= sol_in
        pos = Position(
            mint=mint_str,
            name=event.name,
            symbol=event.symbol,
            entry_price_sol=entry_price,
            token_amount=token_amount,
            sol_spent=sol_in,
            entry_time=time.time(),
            current_price_sol=entry_price,
            current_value_sol=entry_price * token_amount,
        )
        self.positions[mint_str] = pos
        self.trades.append(Trade(
            mint=mint_str, name=event.name, symbol=event.symbol,
            side="BUY", price_sol=entry_price,
            token_amount=token_amount, sol_amount=sol_in,
            timestamp=time.time(),
        ))
        log.info(
            "[PAPER] BUY  %-10s %-6s | %.4f SOL | %s tokens @ %.3e SOL/tok | bal=%.4f SOL",
            event.name[:10], event.symbol, sol_in,
            _fmt_tokens(token_amount), entry_price, self.balance_sol,
        )
        return True

    async def update_positions(self, session: aiohttp.ClientSession) -> None:
        """Poll live bonding curve prices and auto-close migrated positions."""
        from config import RPC_URL
        import base64 as _b64
        from solders.pubkey import Pubkey

        for mint_str, pos in list(self.positions.items()):
            if pos.closed:
                continue
            try:
                mint = Pubkey.from_string(mint_str)
                bc_pda = get_bonding_curve_pda(mint)
                payload = {
                    "jsonrpc": "2.0", "id": 1,
                    "method": "getAccountInfo",
                    "params": [str(bc_pda), {"encoding": "base64", "commitment": "processed"}],
                }
                async with session.post(RPC_URL, json=payload, timeout=aiohttp.ClientTimeout(total=5)) as r:
                    data = await r.json()
                    result = data.get("result", {})
                    if result is None or result.get("value") is None:
                        # Account closed — token migrated, exit at last price
                        pos.migrated = True
                        self._close_position(pos, reason="migrated (account closed)")
                        continue
                    raw_list = result["value"]["data"]
                    bc_bytes = _b64.b64decode(raw_list[0])
                    bc = BondingCurve.decode(bc_bytes)
                    if not bc:
                        continue
                    pos.current_price_sol = bc.token_price_in_sol()
                    pos.current_value_sol = pos.current_price_sol * pos.token_amount
                    pos.pnl_sol = pos.current_value_sol - pos.sol_spent
                    pos.pnl_pct = (pos.pnl_sol / pos.sol_spent * 100) if pos.sol_spent else 0.0
                    if bc.complete:
                        pos.migrated = True
                        self._close_position(pos, reason="migrated (complete flag)")
            except Exception as exc:
                log.debug("position update failed for %s: %s", mint_str[:8], exc)

    def _close_position(self, pos: Position, reason: str = "") -> None:
        if pos.closed:
            return
        pos.closed = True
        pos.exit_price_sol = pos.current_price_sol
        pos.exit_time = time.time()
        pos.exit_value_sol = pos.current_price_sol * pos.token_amount
        pos.pnl_sol = pos.exit_value_sol - pos.sol_spent
        pos.pnl_pct = (pos.pnl_sol / pos.sol_spent * 100) if pos.sol_spent else 0.0
        self.balance_sol += pos.exit_value_sol
        self.trades.append(Trade(
            mint=pos.mint, name=pos.name, symbol=pos.symbol,
            side="SELL", price_sol=pos.exit_price_sol,
            token_amount=pos.token_amount, sol_amount=pos.exit_value_sol,
            timestamp=pos.exit_time, reason=reason,
        ))
        self.closed_positions.append(pos)
        if pos.mint in self.positions:
            del self.positions[pos.mint]
        sign = "+" if pos.pnl_sol >= 0 else ""
        log.info(
            "[PAPER] SELL %-10s %-6s | %.4f SOL | PnL %s%.4f SOL (%s%.1f%%) | %s",
            pos.name[:10], pos.symbol, pos.exit_value_sol,
            sign, pos.pnl_sol, sign, pos.pnl_pct, reason,
        )

    def summary(self) -> str:
        open_unrealized = sum(p.current_value_sol for p in self.positions.values())
        total_value = self.balance_sol + open_unrealized
        total_pnl = total_value - self.starting_balance
        total_pnl_pct = (total_pnl / self.starting_balance * 100) if self.starting_balance else 0.0

        wins = [p for p in self.closed_positions if p.pnl_sol > 0]
        losses = [p for p in self.closed_positions if p.pnl_sol <= 0]
        win_rate = len(wins) / len(self.closed_positions) * 100 if self.closed_positions else 0.0

        lines = [
            "=" * 64,
            " PAPER TRADING SUMMARY",
            f"  Free balance    : {self.balance_sol:.4f} SOL",
            f"  Open positions  : {len(self.positions)}  (unrealized {open_unrealized:.4f} SOL)",
            f"  Total value     : {total_value:.4f} SOL",
            f"  Total P&L       : {total_pnl:+.4f} SOL  ({total_pnl_pct:+.1f}%)",
            f"  Closed trades   : {len(self.closed_positions)}  "
            f"(W:{len(wins)} L:{len(losses)} rate:{win_rate:.0f}%)",
        ]
        if self.positions:
            lines.append("  Open:")
            for p in self.positions.values():
                age = (time.time() - p.entry_time) / 60
                sign = "+" if p.pnl_sol >= 0 else ""
                lines.append(
                    f"    {p.symbol:<8} in={p.sol_spent:.3f} cur={p.current_price_sol:.2e} "
                    f"val={p.current_value_sol:.4f} pnl={sign}{p.pnl_sol:.4f}SOL"
                    f"({sign}{p.pnl_pct:.1f}%) age={age:.1f}m"
                )
        lines.append("=" * 64)
        return "\n".join(lines)

    def save_log(self, path: str = "paper_trades.json") -> None:
        data = {
            "generated_at": time.time(),
            "starting_balance_sol": self.starting_balance,
            "current_balance_sol": self.balance_sol,
            "trades": [asdict(t) for t in self.trades],
            "open_positions": [asdict(p) for p in self.positions.values()],
            "closed_positions": [asdict(p) for p in self.closed_positions],
        }
        with open(path, "w") as f:
            json.dump(data, f, indent=2)


def _fmt_tokens(raw: int) -> str:
    """Format raw token units (6 decimals) as human-readable string."""
    return f"{raw / 1e6:,.2f}"
