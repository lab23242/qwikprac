"""
Paper trading engine.
Records virtual buys/sells, tracks live P&L from bonding curve prices,
and auto-closes positions when a token migrates.
update_positions uses getMultipleAccounts (one round-trip per 100 positions).
"""
import base64
import logging
import time
from dataclasses import dataclass, asdict
from typing import Optional

import aiohttp
import orjson

from pumpfun import BondingCurve, CreateEvent, get_bonding_curve_pda

log = logging.getLogger(__name__)

# pump.fun launch-state price (30 SOL virtual / 1.073B tokens)
_LAUNCH_PRICE = 30e9 / 1_073_000_000_000_000 / 1e9


@dataclass
class Position:
    mint: str
    name: str
    symbol: str
    entry_price_sol: float   # SOL per raw token unit
    token_amount: int        # raw token units (6 decimal places)
    sol_spent: float
    entry_time: float
    market_cap_sol: float = 0.0
    current_price_sol: float = 0.0
    current_value_sol: float = 0.0
    pnl_sol: float = 0.0
    pnl_pct: float = 0.0
    migrated: bool = False
    closed: bool = False
    close_reason: str = ""
    exit_price_sol: float = 0.0
    exit_time: float = 0.0
    exit_value_sol: float = 0.0


@dataclass
class Trade:
    mint: str
    name: str
    symbol: str
    side: str
    price_sol: float
    token_amount: int
    sol_amount: float
    timestamp: float
    reason: str = ""


class PaperTrader:
    def __init__(self, starting_balance_sol: float, buy_amount_sol: float, slippage: float,
                 take_profit_multiple: float = 0.0):
        self.balance_sol          = starting_balance_sol
        self.starting_balance     = starting_balance_sol
        self.buy_amount_sol       = buy_amount_sol
        self.slippage             = slippage
        self.take_profit_multiple = take_profit_multiple
        self.positions:        dict[str, Position] = {}
        self.closed_positions: list[Position]      = []
        self.trades:           list[Trade]         = []

    def buy(self, event: CreateEvent, bc: Optional[BondingCurve]) -> Optional[Position]:
        mint_str = str(event.mint)
        if mint_str in self.positions:
            return None
        if self.balance_sol < self.buy_amount_sol:
            log.warning("[PAPER] Insufficient balance %.4f SOL", self.balance_sol)
            return None

        sol_in = self.buy_amount_sol
        if bc:
            token_amount = bc.tokens_for_sol(sol_in)
            entry_price  = bc.token_price_in_sol()
            mc           = bc.market_cap_sol()
        else:
            entry_price  = _LAUNCH_PRICE
            token_amount = int(sol_in / entry_price) if entry_price else 0
            mc           = 0.0

        self.balance_sol -= sol_in
        pos = Position(
            mint=mint_str, name=event.name, symbol=event.symbol,
            entry_price_sol=entry_price, token_amount=token_amount,
            sol_spent=sol_in, entry_time=time.time(),
            market_cap_sol=mc,
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
            "[PAPER] BUY  %-10s %-8s | %.4f SOL | %s tok @ %.3e | mcap %.1f SOL | bal %.4f",
            event.name[:10], event.symbol, sol_in,
            _fmt(token_amount), entry_price, mc, self.balance_sol,
        )
        return pos

    async def update_positions(self, session: aiohttp.ClientSession) -> list[Position]:
        """Batch-refresh all open positions. Returns list of newly-closed positions."""
        from config import RPC_URL
        from solders.pubkey import Pubkey

        if not self.positions:
            return []

        mint_strs = list(self.positions.keys())
        pdas      = [str(get_bonding_curve_pda(Pubkey.from_string(m))) for m in mint_strs]
        closed_now: list[Position] = []

        for i in range(0, len(pdas), 100):
            chunk_mints = mint_strs[i:i + 100]
            chunk_pdas  = pdas[i:i + 100]
            payload = orjson.dumps({
                "jsonrpc": "2.0", "id": 1,
                "method": "getMultipleAccounts",
                "params": [chunk_pdas, {"encoding": "base64", "commitment": "processed"}],
            })
            try:
                async with session.post(
                    RPC_URL, data=payload,
                    headers={"Content-Type": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as r:
                    data     = orjson.loads(await r.read())
                    accounts = data["result"]["value"]
            except Exception as exc:
                log.debug("update_positions RPC error: %s", exc)
                continue

            for mint_str, acct in zip(chunk_mints, accounts):
                pos = self.positions.get(mint_str)
                if not pos or pos.closed:
                    continue
                if acct is None:
                    pos.migrated = True
                    self._close_position(pos, "migrated (account closed)")
                    closed_now.append(pos)
                    continue
                bc = BondingCurve.decode(base64.b64decode(acct["data"][0]))
                if not bc:
                    continue
                pos.current_price_sol = bc.token_price_in_sol()
                pos.current_value_sol = pos.current_price_sol * pos.token_amount
                pos.pnl_sol           = pos.current_value_sol - pos.sol_spent
                pos.pnl_pct           = (pos.pnl_sol / pos.sol_spent * 100) if pos.sol_spent else 0.0
                if bc.complete:
                    pos.migrated = True
                    self._close_position(pos, "migrated (complete)")
                    closed_now.append(pos)
                elif (self.take_profit_multiple > 0
                      and pos.current_price_sol >= pos.entry_price_sol * self.take_profit_multiple):
                    self._close_position(pos, f"take profit ({self.take_profit_multiple:.1f}×)")
                    closed_now.append(pos)

        return closed_now

    def _close_position(self, pos: Position, reason: str = "") -> None:
        if pos.closed:
            return
        pos.closed          = True
        pos.close_reason    = reason
        pos.exit_price_sol  = pos.current_price_sol
        pos.exit_time       = time.time()
        pos.exit_value_sol  = pos.current_price_sol * pos.token_amount
        pos.pnl_sol         = pos.exit_value_sol - pos.sol_spent
        pos.pnl_pct         = (pos.pnl_sol / pos.sol_spent * 100) if pos.sol_spent else 0.0
        self.balance_sol   += pos.exit_value_sol
        self.trades.append(Trade(
            mint=pos.mint, name=pos.name, symbol=pos.symbol,
            side="SELL", price_sol=pos.exit_price_sol,
            token_amount=pos.token_amount, sol_amount=pos.exit_value_sol,
            timestamp=pos.exit_time, reason=reason,
        ))
        self.closed_positions.append(pos)
        self.positions.pop(pos.mint, None)
        sign = "+" if pos.pnl_sol >= 0 else ""
        log.info(
            "[PAPER] SELL %-10s %-8s | %.4f SOL | PnL %s%.4f (%s%.1f%%) | %s",
            pos.name[:10], pos.symbol, pos.exit_value_sol,
            sign, pos.pnl_sol, sign, pos.pnl_pct, reason,
        )

    def summary(self) -> str:
        unrealized  = sum(p.current_value_sol for p in self.positions.values())
        total_value = self.balance_sol + unrealized
        total_pnl   = total_value - self.starting_balance
        pnl_pct     = (total_pnl / self.starting_balance * 100) if self.starting_balance else 0.0
        wins        = sum(1 for p in self.closed_positions if p.pnl_sol > 0)
        losses      = len(self.closed_positions) - wins
        wr          = f"{wins/len(self.closed_positions)*100:.0f}%" if self.closed_positions else "n/a"
        lines = [
            "=" * 66,
            " PAPER TRADING SUMMARY",
            f"  Balance      : {self.balance_sol:.4f} SOL",
            f"  Open         : {len(self.positions)}  (unrealized {unrealized:.4f} SOL)",
            f"  Total value  : {total_value:.4f} SOL",
            f"  Total P&L    : {total_pnl:+.4f} SOL  ({pnl_pct:+.1f}%)",
            f"  Closed       : {len(self.closed_positions)}  W:{wins} L:{losses} WR:{wr}",
        ]
        for p in self.positions.values():
            age  = (time.time() - p.entry_time) / 60
            sign = "+" if p.pnl_sol >= 0 else ""
            lines.append(
                f"  {p.symbol:<8} val={p.current_value_sol:.4f} "
                f"pnl={sign}{p.pnl_sol:.4f}({sign}{p.pnl_pct:.1f}%) age={age:.1f}m"
            )
        lines.append("=" * 66)
        return "\n".join(lines)

    def summary_for_telegram(self) -> dict:
        unrealized  = sum(p.current_value_sol for p in self.positions.values())
        total_value = self.balance_sol + unrealized
        total_pnl   = total_value - self.starting_balance
        pnl_pct     = (total_pnl / self.starting_balance * 100) if self.starting_balance else 0.0
        wins        = sum(1 for p in self.closed_positions if p.pnl_sol > 0)
        return dict(
            balance=self.balance_sol, open_count=len(self.positions),
            unrealized=unrealized, total_value=total_value,
            total_pnl=total_pnl, total_pnl_pct=pnl_pct,
            closed=len(self.closed_positions), wins=wins,
            losses=len(self.closed_positions) - wins,
        )

    def save_log(self, path: str = "paper_trades.json") -> None:
        data = {
            "generated_at":       time.time(),
            "starting_balance":   self.starting_balance,
            "current_balance":    self.balance_sol,
            "trades":             [asdict(t) for t in self.trades],
            "open_positions":     [asdict(p) for p in self.positions.values()],
            "closed_positions":   [asdict(p) for p in self.closed_positions],
        }
        with open(path, "wb") as f:
            f.write(orjson.dumps(data, option=orjson.OPT_INDENT_2))


def _fmt(raw: int) -> str:
    return f"{raw / 1e6:,.2f}"
