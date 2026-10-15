"""
execution/paper_broker.py
=========================
Paper broker that fills against the *live order book*, not against the LTP.

Why this matters: the single biggest reason paper results fail to survive
contact with a real account is optimistic fills. This simulator is deliberately
pessimistic —

  * a BUY fills at the ask, a SELL fills at the bid (you always cross the spread)
  * configured slippage is then added on top, in ticks
  * when depth is missing the LTP is widened by `assumed_spread_ticks`
  * broker latency is simulated, so an order placed into a moving market gets
    the price that exists after the latency, not before
  * per-order cost is charged so the reported PnL is net

Limit orders behave like real limit orders: they only fill when the book trades
through them, which means the router's chase-and-escalate logic gets exercised
in paper mode exactly as it will in live mode.

SL / SL-M orders are tracked too, so protective stops can be validated on paper.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import config
from core import clock
from core.enums import OrderStatus, Side
from core.logging_setup import get
from data.kite_gateway import KiteGateway
from data.tick_store import TickStore
from execution.broker_base import Broker, OrderRequest, OrderSnapshot, round_to_tick

log = get("paper")


@dataclass
class _PaperOrder:
    order_id: str
    request: OrderRequest
    created_at: float
    status: OrderStatus = OrderStatus.OPEN
    filled_qty: int = 0
    avg_price: float = 0.0
    triggered: bool = False
    message: str = ""
    history: List[str] = field(default_factory=list)


class PaperBroker(Broker):
    mode = "paper"

    def __init__(self, ticks: TickStore, gateway: Optional[KiteGateway] = None) -> None:
        self.ticks = ticks
        self.gw = gateway                  # only used for the real margin API
        self._lock = threading.RLock()
        self._orders: Dict[str, _PaperOrder] = {}
        self._seq = 0
        self._starting_capital = config.PAPER_STARTING_CAPITAL
        self._margin_used = 0.0
        self.realised_pnl = 0.0

    # ─── Order lifecycle ──────────────────────────────────────────────────────

    def _next_id(self) -> str:
        with self._lock:
            self._seq += 1
            return f"PAPER{clock.now().strftime('%H%M%S')}{self._seq:04d}"

    def place(self, request: OrderRequest) -> Optional[str]:
        if request.quantity <= 0:
            log.error("paper place rejected: quantity %s", request.quantity)
            return None

        order_id = self._next_id()
        order = _PaperOrder(
            order_id=order_id,
            request=request,
            created_at=time.monotonic(),
        )
        with self._lock:
            self._orders[order_id] = order

        log.info(
            "PAPER place %s %s %s x%d @ %s%s",
            order_id, request.side, request.tradingsymbol, request.quantity,
            request.order_type,
            f" {request.price:.2f}" if request.order_type in ("LIMIT", "SL") else "",
        )
        self._evaluate(order)
        return order_id

    def status(self, order_id: str) -> OrderSnapshot:
        with self._lock:
            order = self._orders.get(order_id)
        if order is None:
            return OrderSnapshot(order_id=order_id, status=OrderStatus.ERROR,
                                 message="unknown order")
        self._evaluate(order)
        with self._lock:
            return OrderSnapshot(
                order_id=order_id,
                status=order.status,
                filled_qty=order.filled_qty,
                pending_qty=max(0, order.request.quantity - order.filled_qty),
                avg_price=order.avg_price,
                message=order.message,
            )

    def modify(self, order_id: str, price: float, tick_size: float,
               side: Side, critical: bool = False) -> bool:
        with self._lock:
            order = self._orders.get(order_id)
            if order is None or order.status is not OrderStatus.OPEN:
                return False
            order.request.price = round_to_tick(price, tick_size, side)
            order.history.append(f"modify -> {order.request.price:.2f}")
        log.info("PAPER modify %s -> %.2f", order_id, price)
        self._evaluate(order)
        return True

    def cancel(self, order_id: str) -> bool:
        with self._lock:
            order = self._orders.get(order_id)
            if order is None:
                return False
            if order.status is OrderStatus.OPEN:
                order.status = (OrderStatus.PARTIAL if order.filled_qty
                                else OrderStatus.CANCELLED)
                order.message = "cancelled"
                log.info("PAPER cancel %s (filled %d)", order_id, order.filled_qty)
                return True
            return order.status in (OrderStatus.CANCELLED, OrderStatus.PARTIAL)

    # ─── Fill simulation ──────────────────────────────────────────────────────

    def _latency_elapsed(self, order: _PaperOrder) -> bool:
        latency = config.EXECUTION["paper"]["latency_ms"] / 1000.0
        return (time.monotonic() - order.created_at) >= latency

    def _crossing_price(self, request: OrderRequest) -> float:
        """
        Price an aggressive order pays after walking the displayed book.

        A buy lifts asks until the quantity is covered. A sell hits bids the
        same way. Size past the visible ladder is filled at the last level,
        worsened by `thin_book_extra_ticks`, then slippage is added.
        """
        tick = self.ticks.get(request.token)
        if tick is None:
            return 0.0
        vwap, filled = tick.consume(request.side, request.quantity, limit=None)
        if filled <= 0:
            base = self.ticks.executable_price(request.token, request.side, aggressive=True)
            if base <= 0:
                return 0.0
            vwap, filled = base, 0
        if filled < request.quantity:
            last = tick.cover_price(request.side, request.quantity) or vwap
            extra = config.EXECUTION["paper"]["thin_book_extra_ticks"] * request.tick_size
            if request.side is Side.BUY:
                worse = last + extra
            else:
                worse = max(request.tick_size, last - extra)
            if filled > 0:
                vwap = (vwap * filled + worse * (request.quantity - filled)) / request.quantity
            else:
                vwap = worse
        slip = config.EXECUTION["paper"]["slippage_ticks"] * request.tick_size
        if request.side is Side.BUY:
            return vwap + slip
        return max(request.tick_size, vwap - slip)

    def _book_depth_lots(self, request: OrderRequest) -> int:
        tick = self.ticks.get(request.token)
        if tick is None:
            return 0
        return tick.ask_qty if request.side is Side.BUY else tick.bid_qty

    def _evaluate(self, order: _PaperOrder) -> None:
        with self._lock:
            if order.status is not OrderStatus.OPEN:
                return
            if not self._latency_elapsed(order):
                return

            request = order.request
            tick = self.ticks.get(request.token)
            if tick is None or tick.ltp <= 0:
                order.message = "no market data"
                return

            # Stop orders wait for the trigger to be taken out.
            if request.order_type in ("SL", "SL-M") and not order.triggered:
                if request.side is Side.BUY and tick.ltp >= request.trigger_price:
                    order.triggered = True
                    order.history.append(f"triggered at {tick.ltp:.2f}")
                elif request.side is Side.SELL and tick.ltp <= request.trigger_price:
                    order.triggered = True
                    order.history.append(f"triggered at {tick.ltp:.2f}")
                else:
                    return

            fill_price = 0.0

            if request.order_type in ("MARKET", "SL-M") or (
                request.order_type == "SL" and order.triggered
            ):
                fill_price = self._crossing_price(request)
            elif request.order_type == "LIMIT":
                # Fill only when the limit reaches enough of the ladder to
                # cover the quantity. The price is the VWAP of the levels
                # consumed, not the top of book, when the order walks down.
                if tick.has_book or tick.bids or tick.asks:
                    vwap, filled = tick.consume(request.side, request.quantity, limit=request.price)
                    if filled >= request.quantity and vwap > 0:
                        fill_price = vwap
                    elif filled > 0 and tick.cover_price(request.side, request.quantity) > 0:
                        # Visible size is short of the order, but the limit
                        # already reaches the deepest displayed level.
                        cover = tick.cover_price(request.side, request.quantity)
                        reaches = (
                            request.side is Side.BUY and request.price + 1e-9 >= cover
                        ) or (
                            request.side is Side.SELL and request.price - 1e-9 <= cover
                        )
                        if reaches:
                            fill_price = self._crossing_price(request)
                else:
                    crossing = self._crossing_price(request)
                    if crossing > 0:
                        if request.side is Side.BUY and request.price >= crossing:
                            fill_price = crossing
                        elif request.side is Side.SELL and request.price <= crossing:
                            fill_price = crossing

            if fill_price <= 0:
                order.message = "resting"
                return

            min_lots = config.EXECUTION["paper"]["min_depth_lots"]
            if min_lots > 0 and self._book_depth_lots(request) < min_lots:
                order.message = "insufficient depth"
                return

            order.filled_qty = request.quantity
            order.avg_price = round(fill_price, 2)
            order.status = OrderStatus.COMPLETE
            order.message = "filled"
            order.history.append(f"filled {request.quantity} @ {order.avg_price:.2f}")

        log.info("PAPER fill  %s %s %s x%d @ %.2f",
                 order.order_id, order.request.side, order.request.tradingsymbol,
                 order.filled_qty, order.avg_price)

    # ─── Margin ───────────────────────────────────────────────────────────────

    def available_margin(self) -> float:
        return max(0.0, self._starting_capital + self.realised_pnl - self._margin_used)

    def block_margin(self, amount: float) -> None:
        self._margin_used += max(0.0, amount)

    def release_margin(self, amount: float) -> None:
        self._margin_used = max(0.0, self._margin_used - max(0.0, amount))

    def add_realised(self, pnl: float) -> None:
        self.realised_pnl += pnl

    def basket_margin(self, requests: List[OrderRequest]) -> Optional[float]:
        """
        Uses Kite's real margin calculator even in paper mode — it is a read-only
        endpoint, so paper sizing matches what live sizing would be.
        """
        if self.gw is None or not config.EXECUTION.get("use_broker_margin_api"):
            return None
        payload = []
        for request in requests:
            payload.append({
                "exchange": request.exchange,
                "tradingsymbol": request.tradingsymbol,
                "transaction_type": str(request.side),
                "variety": "regular",
                "product": request.product,
                "order_type": "MARKET",
                "quantity": int(request.quantity),
                "price": 0,
                "trigger_price": 0,
            })
        result = self.gw.basket_margin(payload)
        if not result:
            return None
        initial = (result.get("initial") or {})
        total = initial.get("total")
        if total is None:
            total = (result.get("final") or {}).get("total")
        try:
            return float(total) if total is not None else None
        except (TypeError, ValueError):
            return None

    # ─── Diagnostics ──────────────────────────────────────────────────────────

    def open_order_count(self) -> int:
        with self._lock:
            return sum(1 for o in self._orders.values() if o.status is OrderStatus.OPEN)
