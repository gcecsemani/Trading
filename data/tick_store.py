"""
data/tick_store.py
==================
Thread-safe cache sitting between the websocket thread (writer) and the engine
thread (reader).

Two stores:
  TickStore         last tick per instrument, including the 5-level depth so
                    every price decision uses real bid/ask instead of LTP.
  OrderUpdateStore  order postbacks pushed over the same websocket. These
                    arrive faster and more reliably than polling order_history,
                    which matters a lot for stop-loss execution.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, Iterable, List, Optional

import config
from core import clock
from core.enums import Side
from core.logging_setup import get
from core.models import BookLevel, Tick

log = get("ticks")


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _i(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _keep_float(value: Any, previous: float) -> float:
    """Use the new number, unless it is missing or zero and we already have one.

    A full-mode packet sometimes arrives with last_price 0. Overwriting a live
    premium with that zero makes the stop and the target go blind.
    """
    parsed = _f(value)
    if parsed > 0:
        return parsed
    return previous if previous > 0 else 0.0


def _keep_int(value: Any, previous: int) -> int:
    """Same rule for open interest. OI does not fall to zero in the session."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return previous
    if parsed > 0:
        return parsed
    return previous if previous > 0 else 0


def _ladder(rows: Any) -> List[BookLevel]:
    """Kite sends the best price first. Drop empty levels."""
    out: List[BookLevel] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        price = _f(row.get("price"))
        qty = _i(row.get("quantity"))
        if price > 0 and qty > 0:
            out.append(BookLevel(price=price, quantity=qty))
    return out


class TickStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._ticks: Dict[int, Tick] = {}
        self._last_any_ts: float = 0.0
        self._tick_count: int = 0

    # ─── Writers ──────────────────────────────────────────────────────────────

    def apply_ws_ticks(self, raw_ticks: Iterable[Dict[str, Any]]) -> None:
        now_mono = time.monotonic()
        wall = clock.now()
        with self._lock:
            for raw in raw_ticks:
                token = _i(raw.get("instrument_token"))
                if token <= 0:
                    continue

                depth = raw.get("depth") or {}
                bids = _ladder(depth.get("buy"))
                asks = _ladder(depth.get("sell"))
                bid = bids[0].price if bids else 0.0
                ask = asks[0].price if asks else 0.0
                bid_qty = bids[0].quantity if bids else 0
                ask_qty = asks[0].quantity if asks else 0

                ohlc = raw.get("ohlc") or {}
                prev = self._ticks.get(token)

                tick = Tick(
                    token=token,
                    ltp=_keep_float(raw.get("last_price"), prev.ltp if prev else 0.0),
                    bid=bid or (prev.bid if prev else 0.0),
                    ask=ask or (prev.ask if prev else 0.0),
                    bid_qty=bid_qty or (prev.bid_qty if prev else 0),
                    ask_qty=ask_qty or (prev.ask_qty if prev else 0),
                    oi=_keep_int(raw.get("oi"), prev.oi if prev else 0),
                    oi_day_high=_i(raw.get("oi_day_high"), prev.oi_day_high if prev else 0),
                    oi_day_low=_i(raw.get("oi_day_low"), prev.oi_day_low if prev else 0),
                    volume=_i(
                        raw.get("volume_traded", raw.get("volume")),
                        prev.volume if prev else 0,
                    ),
                    open=_f(ohlc.get("open"), prev.open if prev else 0.0),
                    high=_f(ohlc.get("high"), prev.high if prev else 0.0),
                    low=_f(ohlc.get("low"), prev.low if prev else 0.0),
                    close=_f(ohlc.get("close"), prev.close if prev else 0.0),
                    bids=bids or (prev.bids if prev else []),
                    asks=asks or (prev.asks if prev else []),
                    received_at=now_mono,
                    wall_clock=wall,
                )
                self._ticks[token] = tick
                self._tick_count += 1

            self._last_any_ts = now_mono

        if config.LOGGING.get("tick_debug"):
            log.debug("applied %d ticks", len(list(raw_ticks)))

    def apply_rest_quotes(self, quotes: Dict[str, Dict[str, Any]]) -> int:
        """
        Seed / repair the cache from a REST `quote` call. Used at startup (the
        websocket only pushes on change, so an untraded strike would otherwise
        stay empty) and whenever a token looks stale.
        """
        now_mono = time.monotonic()
        wall = clock.now()
        applied = 0
        with self._lock:
            for row in quotes.values():
                token = _i(row.get("instrument_token"))
                if token <= 0:
                    continue
                depth = row.get("depth") or {}
                bids = _ladder(depth.get("buy"))
                asks = _ladder(depth.get("sell"))
                ohlc = row.get("ohlc") or {}
                prev = self._ticks.get(token)
                self._ticks[token] = Tick(
                    token=token,
                    ltp=_keep_float(row.get("last_price"), prev.ltp if prev else 0.0),
                    bid=bids[0].price if bids else (prev.bid if prev else 0.0),
                    ask=asks[0].price if asks else (prev.ask if prev else 0.0),
                    bid_qty=bids[0].quantity if bids else (prev.bid_qty if prev else 0),
                    ask_qty=asks[0].quantity if asks else (prev.ask_qty if prev else 0),
                    bids=bids or (prev.bids if prev else []),
                    asks=asks or (prev.asks if prev else []),
                    oi=_keep_int(row.get("oi"), prev.oi if prev else 0),
                    oi_day_high=_i(row.get("oi_day_high")),
                    oi_day_low=_i(row.get("oi_day_low")),
                    volume=_i(row.get("volume", row.get("volume_traded"))),
                    open=_f(ohlc.get("open")),
                    high=_f(ohlc.get("high")),
                    low=_f(ohlc.get("low")),
                    close=_f(ohlc.get("close")),
                    received_at=now_mono,
                    wall_clock=wall,
                )
                applied += 1
            if applied:
                self._last_any_ts = now_mono
        return applied

    # ─── Readers ──────────────────────────────────────────────────────────────

    def get(self, token: int) -> Optional[Tick]:
        with self._lock:
            return self._ticks.get(int(token))

    def ltp(self, token: int) -> float:
        tick = self.get(token)
        return tick.ltp if tick else 0.0

    def mid(self, token: int) -> float:
        tick = self.get(token)
        return tick.mid if tick else 0.0

    def executable_price(self, token: int, side: Side, aggressive: bool = True) -> float:
        """
        What you would actually transact at right now.

        Falls back to LTP widened by the configured assumed spread when the book
        is missing, so a thin strike can never be priced optimistically.
        """
        tick = self.get(token)
        if tick is None:
            return 0.0
        if tick.has_book:
            return tick.price_for(side, aggressive=aggressive)
        if tick.ltp <= 0:
            return 0.0
        if not aggressive:
            return tick.ltp
        ticks = config.EXECUTION["paper"]["assumed_spread_ticks"]
        # tick_size is per-symbol; 0.05 is the value for every index option today
        half = ticks * 0.05 / 2.0
        return tick.ltp + half if side is Side.BUY else max(0.05, tick.ltp - half)

    def mark_price(self, token: int, side: Side) -> float:
        """
        Conservative mark for MTM: a short leg is marked at the ask (what it
        costs to close), a long leg at the bid. This makes reported PnL
        pessimistic rather than flattering, which is what you want driving a stop.
        """
        closing_side = Side.BUY if side is Side.SELL else Side.SELL
        price = self.executable_price(token, closing_side, aggressive=True)
        if price > 0:
            return price
        return self.ltp(token)

    def tokens(self) -> List[int]:
        with self._lock:
            return list(self._ticks.keys())

    def age(self, token: int) -> float:
        tick = self.get(token)
        return tick.age_sec if tick else 1e9

    def has_seen_tick(self) -> bool:
        with self._lock:
            return self._last_any_ts > 0

    def last_tick_age(self) -> float:
        with self._lock:
            if self._last_any_ts <= 0:
                return 1e9
            return time.monotonic() - self._last_any_ts

    def is_healthy(self) -> bool:
        return self.last_tick_age() <= config.ENGINE["feed_stale_sec"]

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "instruments": len(self._ticks),
                "ticks": self._tick_count,
                "last_tick_age": round(self.last_tick_age(), 2),
            }

    def stale_tokens(self, tokens: Iterable[int], max_age: float) -> List[int]:
        out = []
        with self._lock:
            for token in tokens:
                tick = self._ticks.get(int(token))
                if tick is None or tick.age_sec > max_age or tick.ltp <= 0:
                    out.append(int(token))
        return out


class OrderUpdateStore:
    """Latest websocket postback per order id (live mode fill detection)."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._updates: Dict[str, Dict[str, Any]] = {}

    def apply(self, payload: Dict[str, Any]) -> None:
        order_id = str(payload.get("order_id") or "")
        if not order_id:
            return
        with self._lock:
            self._updates[order_id] = dict(payload)
        log.info(
            "order update %s %s %s filled=%s avg=%s",
            order_id,
            payload.get("tradingsymbol"),
            payload.get("status"),
            payload.get("filled_quantity"),
            payload.get("average_price"),
        )

    def get(self, order_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._updates.get(str(order_id))
            return dict(row) if row else None

    def clear(self, order_id: str) -> None:
        with self._lock:
            self._updates.pop(str(order_id), None)
