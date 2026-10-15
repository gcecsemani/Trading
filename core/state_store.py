"""
core/state_store.py
===================
Durable state so a crash or restart never loses track of an open position.

Three stores, all plain JSON/CSV written atomically:

  engine_state.json   open positions (resumable)
  day_stats.json      today's realised PnL and trade counts
  trades.csv          append-only trade journal
"""

from __future__ import annotations

import csv
import json
import os
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

import config
from core import clock
from core.logging_setup import get

log = get("state")
_LOCK = threading.RLock()


def _ensure_dirs() -> None:
    os.makedirs(config.STATE_DIR, exist_ok=True)
    os.makedirs(config.PATHS["log_dir"], exist_ok=True)


def _read_json(path: str, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, OSError) as exc:
        log.error("Corrupt state file %s (%s) — starting fresh", path, exc)
        try:
            os.replace(path, path + ".corrupt")
        except OSError:
            pass
        return default


def _write_json(path: str, payload: Any) -> None:
    """Atomic write: temp file + os.replace, so a crash cannot truncate state."""
    _ensure_dirs()
    tmp = f"{path}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, default=str)
        os.replace(tmp, path)
    except OSError as exc:
        log.error("Failed writing %s: %s", path, exc)


# ══════════════════════════════════════════════════════════════════════════════
#  Day statistics / lockouts
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class DayStats:
    day: str = field(default_factory=clock.datestamp)
    realised_pnl: float = 0.0
    costs: float = 0.0
    trades: int = 0
    wins: int = 0
    losses: int = 0
    consecutive_losses: int = 0
    last_exit_at: Optional[str] = None
    last_loss_at: Optional[str] = None
    adjustments: int = 0
    flips: int = 0
    order_failures: int = 0
    loss_locked: bool = False
    profit_locked: bool = False
    halted: bool = False
    halt_reason: str = ""
    per_symbol: Dict[str, Dict[str, float]] = field(default_factory=dict)

    # ── helpers ───────────────────────────────────────────────────────────────
    def symbol_row(self, symbol: str) -> Dict[str, float]:
        return self.per_symbol.setdefault(
            symbol, {"pnl": 0.0, "trades": 0, "wins": 0, "losses": 0}
        )

    @property
    def win_rate(self) -> float:
        closed = self.wins + self.losses
        return (self.wins / closed * 100.0) if closed else 0.0

    def last_loss_dt(self) -> Optional[datetime]:
        return datetime.fromisoformat(self.last_loss_at) if self.last_loss_at else None

    def last_exit_dt(self) -> Optional[datetime]:
        return datetime.fromisoformat(self.last_exit_at) if self.last_exit_at else None


class DayStatsStore:
    """Day stats that auto-reset when the calendar date rolls over."""

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path or config.PATHS["day_stats_file"]
        self.stats = self._load()

    def _load(self) -> DayStats:
        raw = _read_json(self.path, None)
        today = clock.datestamp()
        if not raw or raw.get("day") != today:
            if raw:
                log.info("Day rolled over (%s -> %s) — resetting day stats",
                         raw.get("day"), today)
            return DayStats(day=today)
        try:
            return DayStats(**raw)
        except TypeError:
            log.warning("day_stats.json schema changed — resetting")
            return DayStats(day=today)

    def roll_if_needed(self) -> bool:
        if self.stats.day != clock.datestamp():
            log.info("New trading day — resetting day stats")
            self.stats = DayStats(day=clock.datestamp())
            self.save()
            return True
        return False

    def save(self) -> None:
        with _LOCK:
            _write_json(self.path, asdict(self.stats))


# ══════════════════════════════════════════════════════════════════════════════
#  Engine state (open positions, cooldowns)
# ══════════════════════════════════════════════════════════════════════════════

class EngineStateStore:
    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path or config.PATHS["state_file"]

    def load(self) -> Dict[str, Any]:
        data = _read_json(self.path, {}) or {}
        saved_day = data.get("day")
        if saved_day and saved_day != clock.datestamp():
            positions = data.get("positions") or []
            if not positions:
                log.warning(
                    "Ignoring engine_state.json from %s (empty, and not from today)",
                    saved_day,
                )
                return {}
            # A crash or a failed square-off can leave a real spread in this
            # file. Dropping it would leave the broker position unmanaged.
            log.warning(
                "engine_state.json is from %s and still has %d open spread(s)",
                saved_day, len(positions),
            )
            data = dict(data)
            data["stale_day"] = True
            return data
        return data

    def save(self, positions: List[Dict[str, Any]], extra: Optional[Dict[str, Any]] = None) -> None:
        payload = {
            "day": clock.datestamp(),
            "saved_at": clock.now().isoformat(),
            "mode": "live" if config.is_live() else "paper",
            "positions": positions,
        }
        if extra:
            payload.update(extra)
        with _LOCK:
            _write_json(self.path, payload)

    def clear(self) -> None:
        with _LOCK:
            _write_json(self.path, {"day": clock.datestamp(), "positions": []})


# ══════════════════════════════════════════════════════════════════════════════
#  Trade journal
# ══════════════════════════════════════════════════════════════════════════════

#  `pnl` is already NET of `costs`, so never subtract costs again when analysing.
#  mfe/mae are the best and worst marks the position ever printed while open —
#  they are what tells you whether the target is too far and the stop too tight.
_JOURNAL_COLUMNS = [
    "exit_time", "entry_time", "mode", "underlying", "strategy", "expiry", "dte",
    "structure", "lots", "lot_size", "entry_spot", "exit_spot", "spot_move_pct",
    "credit", "target_pnl", "stop_pnl", "margin", "pnl", "pnl_pct_of_credit",
    "return_on_margin_pct", "mfe", "mae", "mfe_pct_of_credit", "mae_pct_of_credit",
    "costs", "exit_reason", "adjustments", "hold_minutes", "entry_score",
    "regime", "bias", "conviction", "iv_rank", "atm_iv", "notes",
]


class TradeJournal:
    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path or config.PATHS["trade_journal"]

    def _rotate_if_header_changed(self) -> bool:
        """
        If the column set has changed since this file was started, appending would
        silently write values under the wrong headings and quietly corrupt a
        month of analysis. Archive the old file instead and start a fresh one.
        Returns True when a new header still needs writing.
        """
        if not os.path.exists(self.path):
            return True
        try:
            with open(self.path, "r", newline="", encoding="utf-8") as fh:
                header = next(csv.reader(fh), [])
        except OSError:
            return False
        if header == _JOURNAL_COLUMNS:
            return False
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        archived = "%s.%s.bak" % (self.path, stamp)
        try:
            os.rename(self.path, archived)
            log.warning("Trade journal columns changed — archived the old file to %s",
                        archived)
        except OSError as exc:
            log.error("Could not archive the old trade journal: %s", exc)
            return False
        return True

    def append(self, row: Dict[str, Any]) -> None:
        _ensure_dirs()
        try:
            need_header = self._rotate_if_header_changed()
            with open(self.path, "a", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=_JOURNAL_COLUMNS,
                                        extrasaction="ignore")
                if need_header:
                    writer.writeheader()
                writer.writerow(row)
        except OSError as exc:
            log.error("Could not append to trade journal: %s", exc)
            return
        try:
            from core.ledger import PerformanceLedger
            PerformanceLedger().refresh()
        except Exception:
            log.exception("P&L ledger refresh failed")
