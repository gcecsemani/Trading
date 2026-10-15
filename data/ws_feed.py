"""
data/ws_feed.py
===============
KiteTicker websocket manager.

Design notes
------------
* MODE_FULL for every option token so we always have the 5-level depth. Bid/ask
  is what the engine prices entries, exits and marks against — never LTP alone.
* The subscription set is managed as a diff. ``ensure()`` is called every scan
  with the tokens currently needed (chain window + all open position legs), and
  only the delta is sent to the exchange.
* Tokens belonging to an open position are pinned so a chain re-centre can never
  unsubscribe an instrument we are still holding risk in.
* Reconnects re-subscribe automatically, and the engine independently watches
  ``TickStore.last_tick_age()`` so a silently dead socket still blocks new entries.
"""

from __future__ import annotations

import threading
from typing import Callable, Iterable, List, Optional, Set

from kiteconnect import KiteTicker

import config
from core.logging_setup import get
from data.tick_store import OrderUpdateStore, TickStore

log = get("ws")

BATCH = 400          # tokens per subscribe call


class WebSocketFeed:
    def __init__(
        self,
        tick_store: TickStore,
        order_store: Optional[OrderUpdateStore] = None,
        on_event: Optional[Callable[[str, str], None]] = None,
    ) -> None:
        self.ticks = tick_store
        self.orders = order_store or OrderUpdateStore()
        self._on_event = on_event

        self._lock = threading.RLock()
        self._desired: Set[int] = set()
        self._pinned: Set[int] = set()
        self._subscribed: Set[int] = set()

        self._connected = threading.Event()
        self._started = False
        self.reconnect_count = 0

        self.kws = KiteTicker(
            api_key=config.ZERODHA_API_KEY,
            access_token=config.ZERODHA_ACCESS_TOKEN,
            reconnect=True,
            reconnect_max_tries=300,
            reconnect_max_delay=30,
        )
        self.kws.on_connect = self._on_connect
        self.kws.on_ticks = self._on_ticks
        self.kws.on_close = self._on_close
        self.kws.on_error = self._on_error
        self.kws.on_reconnect = self._on_reconnect
        self.kws.on_noreconnect = self._on_noreconnect
        self.kws.on_order_update = self._on_order_update

    # ─── Lifecycle ────────────────────────────────────────────────────────────

    def start(self, wait_sec: float = 15.0) -> bool:
        if self._started:
            return self._connected.is_set()
        log.info("Starting websocket…")
        self._started = True
        self.kws.connect(threaded=True)
        connected = self._connected.wait(timeout=wait_sec)
        if not connected:
            log.error("Websocket did not connect within %.0fs", wait_sec)
        return connected

    def stop(self) -> None:
        if not self._started:
            return
        log.info("Closing websocket…")
        try:
            self.kws.close()
        except Exception as exc:                        # pragma: no cover
            log.warning("Error closing websocket: %s", exc)
        self._started = False
        self._connected.clear()

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    # ─── Subscription management ──────────────────────────────────────────────

    def pin(self, tokens: Iterable[int]) -> None:
        """Tokens that must stay subscribed regardless of chain re-centring."""
        with self._lock:
            self._pinned.update(int(t) for t in tokens)
        self._reconcile()

    def unpin(self, tokens: Iterable[int]) -> None:
        with self._lock:
            for token in tokens:
                self._pinned.discard(int(token))

    def ensure(self, tokens: Iterable[int]) -> None:
        """Declare the full set of tokens wanted right now (plus pinned ones)."""
        with self._lock:
            self._desired = {int(t) for t in tokens}
        self._reconcile()

    def add(self, tokens: Iterable[int]) -> None:
        with self._lock:
            self._desired.update(int(t) for t in tokens)
        self._reconcile()

    def discard(self, tokens: Iterable[int]) -> None:
        """Drop tokens that left the chain window. Pinned legs stay subscribed."""
        with self._lock:
            for token in tokens:
                self._desired.discard(int(token))
        self._reconcile()

    def _reconcile(self) -> None:
        with self._lock:
            target = set(self._desired) | set(self._pinned)
            to_add = sorted(target - self._subscribed)
            to_remove = sorted(self._subscribed - target)

        if not self.connected:
            # Nothing to send yet; _on_connect will subscribe the full target.
            return

        if to_add:
            self._subscribe(to_add)
        if to_remove:
            self._unsubscribe(to_remove)

    def _subscribe(self, tokens: List[int]) -> None:
        for start in range(0, len(tokens), BATCH):
            batch = tokens[start:start + BATCH]
            try:
                self.kws.subscribe(batch)
                self.kws.set_mode(self.kws.MODE_FULL, batch)
                with self._lock:
                    self._subscribed.update(batch)
                log.info("Subscribed %d tokens (MODE_FULL), total=%d",
                         len(batch), len(self._subscribed))
            except Exception as exc:
                log.error("subscribe failed for %d tokens: %s", len(batch), exc)

    def _unsubscribe(self, tokens: List[int]) -> None:
        for start in range(0, len(tokens), BATCH):
            batch = tokens[start:start + BATCH]
            try:
                self.kws.unsubscribe(batch)
                with self._lock:
                    self._subscribed.difference_update(batch)
                log.debug("Unsubscribed %d tokens, total=%d",
                          len(batch), len(self._subscribed))
            except Exception as exc:
                log.warning("unsubscribe failed: %s", exc)

    @property
    def subscribed_count(self) -> int:
        with self._lock:
            return len(self._subscribed)

    # ─── Callbacks (run on the websocket thread) ──────────────────────────────

    def _on_connect(self, ws, response) -> None:
        log.info("Websocket connected")
        self._connected.set()
        with self._lock:
            self._subscribed.clear()
            target = sorted(set(self._desired) | set(self._pinned))
        if target:
            self._subscribe(target)
        self._emit("connected", f"{len(target)} tokens")

    def _on_ticks(self, ws, ticks) -> None:
        try:
            self.ticks.apply_ws_ticks(ticks)
        except Exception as exc:                          # pragma: no cover
            log.exception("Error applying ticks: %s", exc)

    def _on_close(self, ws, code, reason) -> None:
        log.warning("Websocket closed: %s %s", code, reason)
        self._connected.clear()
        self._emit("closed", f"{code} {reason}")

    def _on_error(self, ws, code, reason) -> None:
        log.error("Websocket error: %s %s", code, reason)
        self._emit("error", f"{code} {reason}")

    def _on_reconnect(self, ws, attempts_count) -> None:
        self.reconnect_count += 1
        log.warning("Websocket reconnecting (attempt %s)", attempts_count)
        self._emit("reconnecting", f"attempt {attempts_count}")

    def _on_noreconnect(self, ws) -> None:
        log.critical("Websocket gave up reconnecting — data feed is DOWN")
        self._connected.clear()
        self._emit("dead", "reconnect attempts exhausted")

    def _on_order_update(self, ws, data) -> None:
        try:
            self.orders.apply(data)
        except Exception as exc:                          # pragma: no cover
            log.exception("Error applying order update: %s", exc)

    def _emit(self, kind: str, detail: str) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(kind, detail)
        except Exception:                                 # pragma: no cover
            log.debug("feed event handler raised", exc_info=True)
