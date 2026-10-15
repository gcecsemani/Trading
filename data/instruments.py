"""
data/instruments.py
===================
Instrument master for NFO (NIFTY) and BFO (SENSEX).

Downloaded once per day, filtered down to the configured underlyings and cached
on disk so a restart mid-session does not re-download 100k+ rows. The cache
keeps only the expiry being traded, the current week. Later expiries are not
written to the state folder.

Nothing about expiry cycles or lot sizes is hard-coded — expiry dates, strikes
and lot sizes all come from the dump, so exchange changes (SENSEX moving its
weekly expiry day, NIFTY lot-size revisions) need no code change.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Optional, Tuple

import config
from core import clock
from core.enums import OptionType
from core.logging_setup import get
from data.kite_gateway import KiteGateway

log = get("instruments")


def pick_expiry(candidates: List[date]) -> Optional[date]:
    """
    The expiry this session trades.

    On expiry day that is the contract expiring today. On every other day it
    is the nearest expiry inside the configured day range, which is the
    current week.
    """
    if not candidates:
        return None
    ordered = sorted(set(candidates))
    cfg = config.EXPIRY
    if cfg["allow_expiry_day"]:
        today = [expiry for expiry in ordered if clock.dte(expiry) == 0]
        if today:
            return today[0]
    in_window = [
        expiry for expiry in ordered
        if cfg["min_dte"] <= clock.dte(expiry) <= cfg["max_dte"] and clock.dte(expiry) > 0
    ]
    if not in_window:
        return None
    preferred = [expiry for expiry in in_window if clock.dte(expiry) >= cfg["preferred_min_dte"]]
    return preferred[0] if preferred else in_window[0]


@dataclass(frozen=True)
class OptionInstrument:
    token: int
    tradingsymbol: str
    exchange: str
    name: str
    expiry: date
    strike: float
    opt_type: OptionType
    lot_size: int
    tick_size: float


class InstrumentMaster:
    def __init__(self, gateway: KiteGateway) -> None:
        self.gw = gateway
        self._loaded_for: Optional[date] = None
        # symbol -> (expiry, strike, opt_type) -> OptionInstrument
        self._options: Dict[str, Dict[Tuple[date, float, str], OptionInstrument]] = {}
        self._expiries: Dict[str, List[date]] = {}
        self._strikes: Dict[str, Dict[date, List[float]]] = {}
        self._lot_size: Dict[str, int] = {}
        self._spot_token: Dict[str, int] = {}

    # ─── Loading ──────────────────────────────────────────────────────────────

    def _cache_path(self) -> str:
        return os.path.join(config.STATE_DIR, f"instruments_{clock.datestamp()}.json")

    def _prune_old_caches(self) -> None:
        try:
            for name in os.listdir(config.STATE_DIR):
                if name.startswith("instruments_") and name != os.path.basename(self._cache_path()):
                    os.remove(os.path.join(config.STATE_DIR, name))
        except OSError:
            pass

    def ensure_loaded(self, force: bool = False) -> bool:
        if not force and self._loaded_for == clock.today() and self._options:
            return True

        rows = self._load_from_cache()
        from_cache = rows is not None
        if rows is None:
            rows = self._download()
        if not rows:
            log.error("No instruments available — cannot trade")
            return False
        slim = self._current_expiry_only(rows)
        if not slim:
            log.error("No contracts for the current expiry — cannot trade")
            return False
        if not from_cache or len(slim) != len(rows):
            self._save_cache(slim)

        self._index(slim)
        self._loaded_for = clock.today()
        self._prune_old_caches()
        return bool(self._options)

    def _exchanges(self) -> List[str]:
        out = []
        for symbol in config.active_symbols():
            cfg = config.symbol_cfg(symbol)
            for key in ("opt_exchange",):
                exch = cfg.get(key)
                if exch and exch not in out:
                    out.append(exch)
        return out

    def _wanted_names(self) -> Dict[str, str]:
        """instrument 'name' -> our symbol key."""
        return {
            config.symbol_cfg(s)["instrument_name"]: s
            for s in config.active_symbols()
        }

    def _download(self) -> List[dict]:
        wanted = self._wanted_names()
        kept: List[dict] = []
        for exchange in self._exchanges():
            for row in self.gw.instruments(exchange):
                if row.get("name") not in wanted:
                    continue
                if row.get("instrument_type") not in ("CE", "PE", "FUT"):
                    continue
                expiry = clock.as_date(row.get("expiry"))
                if expiry is None or expiry < clock.today():
                    continue
                kept.append({
                    "instrument_token": int(row["instrument_token"]),
                    "tradingsymbol": row["tradingsymbol"],
                    "exchange": row.get("exchange", exchange),
                    "name": row["name"],
                    "expiry": expiry.isoformat(),
                    "strike": float(row.get("strike") or 0.0),
                    "instrument_type": row["instrument_type"],
                    "lot_size": int(row.get("lot_size") or 0),
                    "tick_size": float(row.get("tick_size") or 0.05),
                })
        log.info("Filtered instrument universe: %d contracts before the expiry cut", len(kept))
        return kept

    def _current_expiry_only(self, rows: List[dict]) -> List[dict]:
        """
        CE and PE of the expiry being traded.

        The exchange dump still has to be read in full. Only the current week
        is kept in memory and in the state file. Futures and later expiries
        are left out.
        """
        wanted = self._wanted_names()
        found: Dict[str, List[date]] = {}
        for row in rows:
            symbol = wanted.get(row.get("name"))
            if symbol is None or row.get("instrument_type") not in ("CE", "PE"):
                continue
            expiry = clock.as_date(row.get("expiry"))
            if expiry is None or expiry < clock.today():
                continue
            dates = found.setdefault(symbol, [])
            if expiry not in dates:
                dates.append(expiry)

        chosen: Dict[str, date] = {}
        for symbol, dates in found.items():
            expiry = pick_expiry(dates)
            if expiry is not None:
                chosen[symbol] = expiry
        slim = []
        for row in rows:
            symbol = wanted.get(row.get("name"))
            if symbol is None or row.get("instrument_type") not in ("CE", "PE"):
                continue
            expiry = clock.as_date(row.get("expiry"))
            if expiry is not None and chosen.get(symbol) == expiry:
                slim.append(row)
        if chosen:
            label = ", ".join(f"{name} {day.isoformat()}" for name, day in chosen.items())
            log.info(
                "Current expiry only: %s — %d contracts stored, %d left out",
                label, len(slim), len(rows) - len(slim),
            )
        return slim

    def _load_from_cache(self) -> Optional[List[dict]]:
        path = self._cache_path()
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as fh:
                rows = json.load(fh)
            if not rows:
                return None
            log.info("Loaded %d contracts from today's instrument cache", len(rows))
            return rows
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Instrument cache unreadable (%s) — re-downloading", exc)
            return None

    def _save_cache(self, rows: List[dict]) -> None:
        os.makedirs(config.STATE_DIR, exist_ok=True)
        try:
            with open(self._cache_path(), "w", encoding="utf-8") as fh:
                json.dump(rows, fh)
        except OSError as exc:
            log.warning("Could not write instrument cache: %s", exc)

    def _index(self, rows: List[dict]) -> None:
        wanted = self._wanted_names()
        self._options.clear()
        self._expiries.clear()
        self._strikes.clear()

        for row in rows:
            symbol = wanted.get(row["name"])
            if symbol is None:
                continue
            cfg = config.symbol_cfg(symbol)
            expiry = clock.as_date(row["expiry"])
            if expiry is None:
                continue
            itype = row["instrument_type"]

            if itype not in ("CE", "PE"):
                continue
            if row["exchange"] != cfg["opt_exchange"]:
                continue

            inst = OptionInstrument(
                token=row["instrument_token"],
                tradingsymbol=row["tradingsymbol"],
                exchange=row["exchange"],
                name=row["name"],
                expiry=expiry,
                strike=row["strike"],
                opt_type=OptionType(itype),
                lot_size=row["lot_size"] or cfg["lot_size"],
                tick_size=row["tick_size"] or cfg["tick_size"],
            )
            self._options.setdefault(symbol, {})[(expiry, inst.strike, itype)] = inst
            self._strikes.setdefault(symbol, {}).setdefault(expiry, [])
            if inst.strike not in self._strikes[symbol][expiry]:
                self._strikes[symbol][expiry].append(inst.strike)

        for symbol, per_expiry in self._strikes.items():
            for expiry in per_expiry:
                per_expiry[expiry].sort()
            self._expiries[symbol] = sorted(per_expiry.keys())

        for symbol in self._options:
            sizes = {i.lot_size for i in self._options[symbol].values() if i.lot_size > 0}
            configured = config.symbol_cfg(symbol)["lot_size"]
            if sizes:
                # Most common lot size wins; the dump is authoritative over config.
                self._lot_size[symbol] = max(sizes, key=lambda s: sum(
                    1 for i in self._options[symbol].values() if i.lot_size == s))
                if self._lot_size[symbol] != configured:
                    log.warning(
                        "%s lot size from exchange is %d (config says %d) — using %d",
                        symbol, self._lot_size[symbol], configured, self._lot_size[symbol],
                    )
            else:
                self._lot_size[symbol] = configured
            log.info("%s: %d option contracts, expiries %s, lot=%d",
                     symbol, len(self._options[symbol]),
                     [e.isoformat() for e in self._expiries.get(symbol, [])[:4]],
                     self._lot_size[symbol])

    # ─── Accessors ────────────────────────────────────────────────────────────

    def has_symbol(self, symbol: str) -> bool:
        return bool(self._options.get(symbol))

    def lot_size(self, symbol: str) -> int:
        return self._lot_size.get(symbol, config.symbol_cfg(symbol)["lot_size"])

    def expiries(self, symbol: str) -> List[date]:
        return list(self._expiries.get(symbol, []))

    def select_expiry(self, symbol: str) -> Optional[date]:
        """The current week's expiry. See pick_expiry."""
        return pick_expiry(self.expiries(symbol))

    def strikes(self, symbol: str, expiry: date) -> List[float]:
        return list(self._strikes.get(symbol, {}).get(expiry, []))

    def option(
        self, symbol: str, expiry: date, strike: float, opt_type: OptionType
    ) -> Optional[OptionInstrument]:
        return self._options.get(symbol, {}).get((expiry, float(strike), str(opt_type)))

    def strike_window(
        self, symbol: str, expiry: date, atm: float, each_side: int
    ) -> List[OptionInstrument]:
        """Every CE and PE within `each_side` strike steps of ATM."""
        step = config.symbol_cfg(symbol)["strike_step"]
        lo, hi = atm - each_side * step, atm + each_side * step
        out: List[OptionInstrument] = []
        for strike in self.strikes(symbol, expiry):
            if strike < lo or strike > hi:
                continue
            for opt_type in (OptionType.CE, OptionType.PE):
                inst = self.option(symbol, expiry, strike, opt_type)
                if inst is not None:
                    out.append(inst)
        return out

    def round_to_strike(self, symbol: str, price: float) -> float:
        step = config.symbol_cfg(symbol)["strike_step"]
        return round(price / step) * step

    def atm_strike(self, symbol: str, expiry: date, spot: float) -> float:
        """Nearest strike that actually exists in the chain."""
        available = self.strikes(symbol, expiry)
        if not available:
            return self.round_to_strike(symbol, spot)
        return min(available, key=lambda s: abs(s - spot))

    # ─── Underlying spot token (needed for historical candles) ────────────────

    def spot_token(self, symbol: str) -> Optional[int]:
        if symbol in self._spot_token:
            return self._spot_token[symbol]
        key = config.symbol_cfg(symbol)["spot_key"]
        data = self.gw.ltp([key])
        row = data.get(key) or {}
        token = row.get("instrument_token")
        if token:
            self._spot_token[symbol] = int(token)
            return int(token)
        log.error("Could not resolve spot instrument token for %s (key=%s)", symbol, key)
        return None
