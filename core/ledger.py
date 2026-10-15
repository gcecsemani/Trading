"""
core/ledger.py
==============
Daily and monthly P&L ledgers, rebuilt from the trade journal.

state/trades.csv is the trade-by-trade record. These two files are the
scoreboard you open at the end of a day and at the end of a month:

    state/ledger_daily.csv      one row per day
    state/ledger_monthly.csv    one row per calendar month

`net_pnl` is already net of costs. A day the engine ran but did not trade is
written as FLAT so a quiet session is visible rather than missing.
"""

from __future__ import annotations

import csv
import os
from typing import Dict, List

import config
from core.logging_setup import get

log = get("ledger")

_DAILY_COLUMNS = [
    "date", "mode", "trades", "wins", "losses", "win_rate_pct",
    "net_pnl", "costs", "nifty_pnl", "nifty_trades", "sensex_pnl", "sensex_trades",
    "result",
]

_MONTHLY_COLUMNS = [
    "month", "trading_days", "trades", "wins", "losses", "win_rate_pct",
    "net_pnl", "costs", "nifty_pnl", "sensex_pnl",
    "best_day", "best_day_pnl", "worst_day", "worst_day_pnl", "result",
]


def _num(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _result(pnl: float) -> str:
    if pnl > 0:
        return "PROFIT"
    if pnl < 0:
        return "LOSS"
    return "FLAT"


def _write(path: str, columns: List[str], rows: List[Dict[str, object]]) -> None:
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    os.replace(tmp, path)


class PerformanceLedger:
    def __init__(self) -> None:
        self.journal_path = config.PATHS["trade_journal"]
        self.daily_path = config.PATHS["ledger_daily"]
        self.monthly_path = config.PATHS["ledger_monthly"]

    def refresh(self) -> None:
        """Rebuild both ledgers from the journal, keeping recorded no-trade days."""
        try:
            days = self._from_journal()
            for row in self._read(self.daily_path):
                if _num(row.get("trades")) == 0 and row.get("date") not in days:
                    days[str(row["date"])] = row
            self._publish(days)
        except OSError as exc:
            log.error("Could not refresh the P&L ledger: %s", exc)

    def ensure_day(self, day: str) -> None:
        """
        Make sure `day` has a row. A session that closed with no trades is
        recorded as FLAT instead of being absent from the month.
        """
        try:
            self.refresh()
            days = {str(row.get("date")): row for row in self._read(self.daily_path)}
            if day in days:
                return
            mode = "live" if config.is_live() else "paper"
            days[day] = self._blank_day(day, mode)
            self._publish(days)
        except OSError as exc:
            log.error("Could not record %s on the daily ledger: %s", day, exc)

    # ── internals ────────────────────────────────────────────────────────────

    def _publish(self, days: Dict[str, Dict[str, object]]) -> None:
        ordered = [days[key] for key in sorted(days)]
        _write(self.daily_path, _DAILY_COLUMNS, ordered)
        _write(self.monthly_path, _MONTHLY_COLUMNS, self._months(ordered))

    def _from_journal(self) -> Dict[str, Dict[str, object]]:
        grouped: Dict[str, List[Dict[str, str]]] = {}
        if not os.path.exists(self.journal_path):
            return {}
        with open(self.journal_path, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                stamp = str(row.get("exit_time") or "")
                if len(stamp) < 10:
                    continue
                grouped.setdefault(stamp[:10], []).append(row)

        days: Dict[str, Dict[str, object]] = {}
        for day, trades in grouped.items():
            days[day] = self._summarise_day(day, trades)
        return days

    def _summarise_day(self, day: str, trades: List[Dict[str, str]]) -> Dict[str, object]:
        pnl = sum(_num(row.get("pnl")) for row in trades)
        costs = sum(_num(row.get("costs")) for row in trades)
        wins = sum(1 for row in trades if _num(row.get("pnl")) >= 0)
        losses = len(trades) - wins
        modes = {str(row.get("mode") or "") for row in trades if row.get("mode")}
        mode = next(iter(modes)) if len(modes) == 1 else ("mixed" if modes else "")

        def symbol_pnl(name: str) -> float:
            return sum(_num(row.get("pnl")) for row in trades
                       if str(row.get("underlying") or "").upper() == name)

        def symbol_count(name: str) -> int:
            return sum(1 for row in trades
                       if str(row.get("underlying") or "").upper() == name)

        return {
            "date": day,
            "mode": mode,
            "trades": len(trades),
            "wins": wins,
            "losses": losses,
            "win_rate_pct": round(wins / len(trades) * 100.0, 1) if trades else "",
            "net_pnl": round(pnl, 2),
            "costs": round(costs, 2),
            "nifty_pnl": round(symbol_pnl("NIFTY"), 2),
            "nifty_trades": symbol_count("NIFTY"),
            "sensex_pnl": round(symbol_pnl("SENSEX"), 2),
            "sensex_trades": symbol_count("SENSEX"),
            "result": _result(pnl),
        }

    def _blank_day(self, day: str, mode: str) -> Dict[str, object]:
        return {
            "date": day,
            "mode": mode,
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate_pct": "",
            "net_pnl": 0.0,
            "costs": 0.0,
            "nifty_pnl": 0.0,
            "nifty_trades": 0,
            "sensex_pnl": 0.0,
            "sensex_trades": 0,
            "result": "FLAT",
        }

    def _months(self, days: List[Dict[str, object]]) -> List[Dict[str, object]]:
        grouped: Dict[str, List[Dict[str, object]]] = {}
        for row in days:
            date = str(row.get("date") or "")
            if len(date) < 7:
                continue
            grouped.setdefault(date[:7], []).append(row)

        out = []
        for month in sorted(grouped):
            rows = grouped[month]
            pnl = sum(_num(row.get("net_pnl")) for row in rows)
            trades = int(sum(_num(row.get("trades")) for row in rows))
            wins = int(sum(_num(row.get("wins")) for row in rows))
            losses = int(sum(_num(row.get("losses")) for row in rows))
            best = max(rows, key=lambda row: _num(row.get("net_pnl")))
            worst = min(rows, key=lambda row: _num(row.get("net_pnl")))
            out.append({
                "month": month,
                "trading_days": len(rows),
                "trades": trades,
                "wins": wins,
                "losses": losses,
                "win_rate_pct": round(wins / trades * 100.0, 1) if trades else "",
                "net_pnl": round(pnl, 2),
                "costs": round(sum(_num(row.get("costs")) for row in rows), 2),
                "nifty_pnl": round(sum(_num(row.get("nifty_pnl")) for row in rows), 2),
                "sensex_pnl": round(sum(_num(row.get("sensex_pnl")) for row in rows), 2),
                "best_day": best.get("date", ""),
                "best_day_pnl": best.get("net_pnl", 0),
                "worst_day": worst.get("date", ""),
                "worst_day_pnl": worst.get("net_pnl", 0),
                "result": _result(pnl),
            })
        return out

    def _read(self, path: str) -> List[Dict[str, str]]:
        if not os.path.exists(path):
            return []
        with open(path, newline="", encoding="utf-8") as fh:
            return list(csv.DictReader(fh))
