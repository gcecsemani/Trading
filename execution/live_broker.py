"""
execution/live_broker.py
========================
Real order placement through Kite.

Fill detection uses the websocket order postback first (``OrderUpdateStore``) and
falls back to ``order_history``. The postback arrives within milliseconds of the
exchange confirming, which is the difference between a stop-loss that exits on
time and one that exits after the next REST poll.

Every live order carries a tag so positions opened by this engine can always be
told apart from anything you do manually in Kite.
"""

from __future__ import annotations

from typing import Dict, List, Optional

import config
from core.enums import OrderStatus, Side
from core.logging_setup import get
from data.kite_gateway import KiteGateway
from data.tick_store import OrderUpdateStore
from execution.broker_base import Broker, OrderRequest, OrderSnapshot, round_to_tick

log = get("live")

_STATUS_MAP = {
    "COMPLETE": OrderStatus.COMPLETE,
    "REJECTED": OrderStatus.REJECTED,
    "CANCELLED": OrderStatus.CANCELLED,
    "OPEN": OrderStatus.OPEN,
    "TRIGGER PENDING": OrderStatus.OPEN,
    "VALIDATION PENDING": OrderStatus.OPEN,
    "PUT ORDER REQ RECEIVED": OrderStatus.OPEN,
    "MODIFY VALIDATION PENDING": OrderStatus.OPEN,
    "MODIFY PENDING": OrderStatus.OPEN,
    "OPEN PENDING": OrderStatus.OPEN,
    "CANCEL PENDING": OrderStatus.OPEN,
    "AMO REQ RECEIVED": OrderStatus.OPEN,
}


def prefer_snapshot(websocket: OrderSnapshot, history: OrderSnapshot) -> OrderSnapshot:
    """
    Pick the fill report that is further along.

    The websocket postback is usually first, but a missed COMPLETE update must
    not hide a fill that order history already shows. Trusting the stale OPEN
    postback is how a second market order gets sent for size that already filled.
    """
    if history.filled_qty > websocket.filled_qty:
        return history
    if history.is_complete and not websocket.is_complete:
        return history
    if (
        history.status is OrderStatus.REJECTED
        and websocket.status is OrderStatus.OPEN
        and history.filled_qty >= websocket.filled_qty
    ):
        return history
    return websocket


def _map_status(raw: str, filled: int, requested: int) -> OrderStatus:
    status = _STATUS_MAP.get((raw or "").upper().strip(), OrderStatus.OPEN)
    if status is OrderStatus.CANCELLED and 0 < filled < requested:
        return OrderStatus.PARTIAL
    return status


class LiveBroker(Broker):
    mode = "live"

    def __init__(self, gateway: KiteGateway, order_updates: OrderUpdateStore) -> None:
        self.gw = gateway
        self.updates = order_updates
        self._requested: Dict[str, int] = {}
        self._variety: Dict[str, str] = {}

    # ─── Order lifecycle ──────────────────────────────────────────────────────

    def place(self, request: OrderRequest) -> Optional[str]:
        if request.quantity <= 0:
            log.error("live place rejected: quantity %s", request.quantity)
            return None

        params = request.kite_params()
        log.info("LIVE place %s %s x%d %s%s",
                 request.side, request.tradingsymbol, request.quantity,
                 request.order_type,
                 f" @ {params.get('price')}" if "price" in params else "")

        order_id = self.gw.place_order(**params)
        if not order_id:
            return None
        self._requested[str(order_id)] = int(request.quantity)
        self._variety[str(order_id)] = request.variety
        return str(order_id)

    def status(self, order_id: str) -> OrderSnapshot:
        requested = self._requested.get(str(order_id), 0)

        postback = self.updates.get(order_id)
        if postback:
            filled = int(postback.get("filled_quantity") or 0)
            snapshot = OrderSnapshot(
                order_id=order_id,
                status=_map_status(postback.get("status", ""), filled, requested),
                filled_qty=filled,
                pending_qty=int(postback.get("pending_quantity") or 0),
                avg_price=float(postback.get("average_price") or 0.0),
                message=postback.get("status_message") or "",
            )
            if snapshot.is_final:
                return snapshot
            # Non-final postback: still return it, it is the freshest truth.
            return snapshot

        return self._from_history(order_id, requested) or OrderSnapshot(
            order_id=order_id, status=OrderStatus.OPEN, message="no history yet",
        )

    def status_rest(self, order_id: str) -> OrderSnapshot:
        """Order history only. Used before a market fallback, not on every poll."""
        requested = self._requested.get(str(order_id), 0)
        return self._from_history(order_id, requested) or OrderSnapshot(
            order_id=order_id, status=OrderStatus.OPEN, message="no history yet",
        )

    def _from_history(self, order_id: str, requested: int) -> Optional[OrderSnapshot]:
        history = self.gw.order_history(order_id)
        if not history:
            return None
        last = history[-1]
        filled = int(last.get("filled_quantity") or 0)
        return OrderSnapshot(
            order_id=order_id,
            status=_map_status(last.get("status", ""), filled, requested),
            filled_qty=filled,
            pending_qty=int(last.get("pending_quantity") or 0),
            avg_price=float(last.get("average_price") or 0.0),
            message=last.get("status_message") or "",
        )

    def modify(self, order_id: str, price: float, tick_size: float,
               side: Side, critical: bool = False) -> bool:
        return self.gw.modify_order(
            order_id,
            critical=critical,
            variety=self._variety.get(str(order_id), config.EXECUTION["variety"]),
            price=round_to_tick(price, tick_size, side),
            order_type="LIMIT",
        )

    def cancel(self, order_id: str) -> bool:
        return self.gw.cancel_order(
            order_id, variety=self._variety.get(str(order_id))
        )

    # ─── Margin ───────────────────────────────────────────────────────────────

    def available_margin(self) -> float:
        return self.gw.available_margin()

    def basket_margin(self, requests: List[OrderRequest]) -> Optional[float]:
        if not config.EXECUTION.get("use_broker_margin_api"):
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
        initial = result.get("initial") or {}
        total = initial.get("total")
        if total is None:
            total = (result.get("final") or {}).get("total")
        try:
            return float(total) if total is not None else None
        except (TypeError, ValueError):
            return None

    # ─── Reconciliation ───────────────────────────────────────────────────────

    def open_orders(self) -> List[Dict]:
        tag = "optsell"
        return [o for o in self.gw.orders()
                if (o.get("tag") or "").startswith(tag)
                and (o.get("status") or "").upper() in ("OPEN", "TRIGGER PENDING")]

    def cancel_all_engine_orders(self) -> int:
        cancelled = 0
        for order in self.open_orders():
            if self.gw.cancel_order(str(order.get("order_id")),
                                    variety=order.get("variety")):
                cancelled += 1
        if cancelled:
            log.warning("Cancelled %d stale engine orders", cancelled)
        return cancelled
