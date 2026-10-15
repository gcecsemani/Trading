"""
execution/broker_base.py
========================
Broker abstraction. The order router talks only to this interface, so paper and
live execution follow exactly the same code path — the only difference is where
the fill comes from. That is deliberate: a bug that only appears in live mode is
the most expensive kind of bug in a trading system.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import config
from core.enums import OrderStatus, Side


def round_to_tick(price: float, tick: float, side: Optional[Side] = None) -> float:
    """
    Snap a price to the instrument tick. Rounds in the direction that helps the
    order fill: a BUY rounds up, a SELL rounds down.
    """
    if tick <= 0:
        return round(price, 2)
    if price <= 0:
        return tick
    steps = price / tick
    if side is Side.BUY:
        snapped = math.ceil(steps - 1e-9) * tick
    elif side is Side.SELL:
        snapped = math.floor(steps + 1e-9) * tick
    else:
        snapped = round(steps) * tick
    return round(max(tick, snapped), 2)


@dataclass
class OrderRequest:
    exchange: str
    tradingsymbol: str
    token: int
    side: Side
    quantity: int
    order_type: str = "LIMIT"            # LIMIT | MARKET | SL | SL-M
    price: float = 0.0
    trigger_price: float = 0.0
    product: str = field(default_factory=lambda: config.EXECUTION["product"])
    variety: str = field(default_factory=lambda: config.EXECUTION["variety"])
    tick_size: float = 0.05
    tag: str = "optsell"

    def kite_params(self) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "variety": self.variety,
            "exchange": self.exchange,
            "tradingsymbol": self.tradingsymbol,
            "transaction_type": str(self.side),
            "quantity": int(self.quantity),
            "product": self.product,
            "order_type": self.order_type,
            "tag": self.tag[:20],
        }
        if self.order_type in ("LIMIT", "SL"):
            params["price"] = round_to_tick(self.price, self.tick_size, self.side)
        if self.order_type in ("SL", "SL-M"):
            params["trigger_price"] = round_to_tick(self.trigger_price, self.tick_size)
        return params


@dataclass
class OrderSnapshot:
    order_id: str
    status: OrderStatus = OrderStatus.OPEN
    filled_qty: int = 0
    pending_qty: int = 0
    avg_price: float = 0.0
    message: str = ""

    @property
    def is_final(self) -> bool:
        return self.status in (
            OrderStatus.COMPLETE, OrderStatus.CANCELLED,
            OrderStatus.REJECTED, OrderStatus.ERROR,
        )

    @property
    def is_complete(self) -> bool:
        return self.status is OrderStatus.COMPLETE


class Broker(ABC):
    mode: str = "abstract"

    @abstractmethod
    def place(self, request: OrderRequest) -> Optional[str]:
        """Submit an order; returns the broker order id or None on failure."""

    @abstractmethod
    def status(self, order_id: str) -> OrderSnapshot:
        """Current state of an order."""

    @abstractmethod
    def modify(self, order_id: str, price: float, tick_size: float,
               side: Side, critical: bool = False) -> bool:
        """Reprice a resting limit order."""

    @abstractmethod
    def cancel(self, order_id: str) -> bool:
        """Cancel a resting order."""

    @abstractmethod
    def available_margin(self) -> float:
        """Free capital usable for new positions."""

    def basket_margin(self, requests: List[OrderRequest]) -> Optional[float]:
        """Total margin required for a basket, hedge benefit included."""
        return None

    def costs_per_order(self) -> float:
        if self.mode == "paper":
            return config.EXECUTION["paper"]["cost_per_order"]
        return config.EXECUTION["cost_per_order_live"]
