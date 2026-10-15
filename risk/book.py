"""
risk/book.py
============
Day scoreboard. It records closed trades and nothing else.

There is no daily loss lock, no trade-count lock and no margin cap. Those
were part of the previous strategy and are intentionally not applied while
this one is being verified on paper.
"""

from __future__ import annotations

from typing import Dict, Optional

import config
from core import clock
from core.logging_setup import get
from core.models import Position
from core.state_store import DayStats, DayStatsStore, TradeJournal

log = get("book")


class TradeBook:
    def __init__(
        self,
        day_store: Optional[DayStatsStore] = None,
        journal: Optional[TradeJournal] = None,
    ) -> None:
        self.day_store = day_store or DayStatsStore()
        self.journal = journal or TradeJournal()

    @property
    def stats(self) -> DayStats:
        return self.day_store.stats

    def roll_day_if_needed(self) -> bool:
        return self.day_store.roll_if_needed()

    def day_pnl(self, open_positions: list) -> float:
        return self.stats.realised_pnl + sum(p.total_pnl() for p in open_positions)

    def register_entry(self) -> None:
        self.stats.trades += 1
        self.day_store.save()

    def register_exit(self, position: Position) -> None:
        pnl = position.total_pnl()
        stats = self.stats
        stats.realised_pnl += pnl
        stats.costs += position.costs
        stats.last_exit_at = clock.now().isoformat()
        if pnl >= 0:
            stats.wins += 1
            stats.consecutive_losses = 0
        else:
            stats.losses += 1
            stats.consecutive_losses += 1
            stats.last_loss_at = clock.now().isoformat()

        row = stats.symbol_row(position.underlying)
        row["pnl"] = float(row.get("pnl", 0.0)) + pnl
        row["trades"] = float(row.get("trades", 0.0)) + 1
        if pnl >= 0:
            row["wins"] = float(row.get("wins", 0)) + 1
        else:
            row["losses"] = float(row.get("losses", 0)) + 1

        self.day_store.save()
        self.journal.append(_journal_row(position, pnl))
        log.info(
            "closed %s pnl %.0f  day %.0f  (%dW / %dL)",
            position.id, pnl, stats.realised_pnl, stats.wins, stats.losses,
        )

    def summary(self) -> Dict[str, object]:
        stats = self.stats
        return {
            "day": stats.day,
            "trades": stats.trades,
            "wins": stats.wins,
            "losses": stats.losses,
            "realised_pnl": round(stats.realised_pnl, 2),
            "costs": round(stats.costs, 2),
        }


def _journal_row(position: Position, pnl: float) -> Dict[str, object]:
    view = position.entry_view or {}
    credit = position.credit if position.credit > 0 else 0.0

    def pct(value: float) -> object:
        return round(value / credit * 100.0, 2) if credit else ""

    spot_move = ""
    if position.entry_spot > 0 and position.exit_spot > 0:
        spot_move = round(
            (position.exit_spot - position.entry_spot) / position.entry_spot * 100.0, 3
        )
    margin = position.margin_blocked
    return {
        "exit_time": position.exit_time.isoformat() if position.exit_time else "",
        "entry_time": position.entry_time.isoformat() if position.entry_time else "",
        "mode": "live" if config.is_live() else "paper",
        "underlying": position.underlying,
        "strategy": position.strategy,
        "expiry": position.expiry.isoformat() if position.expiry else "",
        "dte": clock.dte(position.expiry) if position.expiry else "",
        "structure": position.describe(),
        "lots": position.lots,
        "lot_size": position.lot_size,
        "entry_spot": round(position.entry_spot, 2),
        "exit_spot": round(position.exit_spot, 2),
        "spot_move_pct": spot_move,
        "credit": round(position.credit, 2),
        "target_pnl": round(position.target_pnl, 2),
        "stop_pnl": round(position.stop_pnl, 2),
        "margin": round(margin, 2),
        "pnl": round(pnl, 2),
        "pnl_pct_of_credit": round(position.pnl_pct_of_credit() * 100.0, 2) if credit else "",
        "return_on_margin_pct": round(pnl / margin * 100.0, 3) if margin > 0 else "",
        "mfe": round(position.peak_pnl, 2),
        "mae": round(position.trough_pnl, 2),
        "mfe_pct_of_credit": pct(position.peak_pnl),
        "mae_pct_of_credit": pct(position.trough_pnl),
        "costs": round(position.costs, 2),
        "exit_reason": str(position.exit_reason or ""),
        "adjustments": len(position.adjustments),
        "hold_minutes": round(position.age_minutes(), 1),
        "entry_score": round(position.entry_score, 1),
        "regime": view.get("span", "60m"),
        "bias": view.get("bias", ""),
        "conviction": view.get("votes", ""),
        "iv_rank": "",
        "atm_iv": "",
        "notes": view.get("notes", ""),
    }
