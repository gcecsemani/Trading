"""
core/models.py
==============
Every dataclass the engine passes around. Kept in one module so analytics,
strategies, execution and risk all share identical shapes and there is exactly
one place that defines how PnL is computed.

PnL convention (per leg, in rupees):
    SELL :  (entry_price - mark) * quantity
    BUY  :  (mark - entry_price) * quantity
A short option therefore profits as the premium decays, which is what the
whole engine is built around.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from core import clock
from core.enums import (
    AdjustAction,
    ExitReason,
    LegRole,
    LegState,
    OptionType,
    PositionState,
    Side,
)


def new_id(prefix: str) -> str:
    return f"{prefix}-{clock.now().strftime('%H%M%S')}-{uuid.uuid4().hex[:4]}"


@dataclass
class BookLevel:
    """One price level on the bid or ask ladder. Quantity is in units, not lots."""

    price: float
    quantity: int


# ══════════════════════════════════════════════════════════════════════════════
#  Market data
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class Tick:
    """One instrument's live state, sourced from the websocket (MODE_FULL)."""

    token: int
    ltp: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    bid_qty: int = 0
    ask_qty: int = 0
    oi: int = 0
    oi_day_high: int = 0
    oi_day_low: int = 0
    volume: int = 0
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0          # previous day close
    bids: List[BookLevel] = field(default_factory=list)   # best (highest) first
    asks: List[BookLevel] = field(default_factory=list)   # best (lowest) first
    received_at: float = field(default_factory=time.monotonic)
    wall_clock: datetime = field(default_factory=clock.now)

    @property
    def age_sec(self) -> float:
        return time.monotonic() - self.received_at

    @property
    def has_book(self) -> bool:
        return self.bid > 0 and self.ask > 0 and self.ask >= self.bid

    @property
    def mid(self) -> float:
        if self.has_book:
            return (self.bid + self.ask) / 2.0
        return self.ltp

    @property
    def spread(self) -> float:
        return (self.ask - self.bid) if self.has_book else 0.0

    @property
    def spread_pct(self) -> float:
        m = self.mid
        if m <= 0 or not self.has_book:
            return 999.0
        return self.spread / m * 100.0

    def _levels(self, side: Side) -> List[BookLevel]:
        """The ladder a marketable order walks. BUY lifts asks, SELL hits bids."""
        return self.asks if side is Side.BUY else self.bids

    def depth_qty(self, side: Side) -> int:
        levels = self._levels(side)
        total = sum(level.quantity for level in levels if level.quantity > 0)
        if total > 0:
            return total
        return self.ask_qty if side is Side.BUY else self.bid_qty

    def imbalance(self) -> Optional[float]:
        """
        (bid qty - ask qty) / (bid qty + ask qty) across the whole displayed book.

        Positive means buyers are stacked. Negative means sellers are offering.
        None when the book is empty.
        """
        bid = self.depth_qty(Side.SELL)
        ask = self.depth_qty(Side.BUY)
        if bid + ask <= 0:
            return None
        return (bid - ask) / (bid + ask)

    def cover_price(self, side: Side, quantity: int) -> float:
        """
        The price level you have to reach so the displayed book covers `quantity`.

        A buy walks up the asks; a sell walks down the bids. If the visible book
        is thinner than the order, this is the deepest displayed price — the
        fill model then worsens the remainder past that level.
        """
        levels = [level for level in self._levels(side) if level.price > 0 and level.quantity > 0]
        if not levels:
            return self.ask if side is Side.BUY else self.bid
        need = max(1, int(quantity))
        seen = 0
        price = levels[-1].price
        for level in levels:
            seen += level.quantity
            price = level.price
            if seen >= need:
                return price
        return price

    def consume(
        self, side: Side, quantity: int, limit: Optional[float] = None
    ) -> Tuple[float, int]:
        """
        Walk the book and return (vwap, units_filled).

        `limit` stops the walk: a buy ignores asks above it, a sell ignores bids
        below it. The returned fill can be smaller than `quantity` when the
        displayed size runs out.
        """
        levels = [level for level in self._levels(side) if level.price > 0 and level.quantity > 0]
        need = max(0, int(quantity))
        notional = 0.0
        filled = 0
        for level in levels:
            if limit is not None:
                if side is Side.BUY and level.price > limit + 1e-9:
                    break
                if side is Side.SELL and level.price < limit - 1e-9:
                    break
            take = min(level.quantity, need - filled)
            if take <= 0:
                break
            notional += level.price * take
            filled += take
            if filled >= need:
                break
        if filled <= 0:
            return 0.0, 0
        return notional / filled, filled

    def price_for(self, side: Side, aggressive: bool = True) -> float:
        """
        Executable price for `side`.

        aggressive=True  -> cross the spread (BUY at ask, SELL at bid). This is
                            what you actually pay, and what marks are based on.
        aggressive=False -> rest at the mid.
        """
        if not self.has_book:
            return self.ltp
        if not aggressive:
            return self.mid
        return self.ask if side is Side.BUY else self.bid


@dataclass
class ChainRow:
    """One option contract inside the chain snapshot."""

    token: int
    tradingsymbol: str
    exchange: str
    strike: float
    opt_type: OptionType
    expiry: date
    lot_size: int
    tick_size: float

    ltp: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    bid_depth: int = 0          # sum of displayed bid quantities
    ask_depth: int = 0          # sum of displayed ask quantities
    oi: int = 0


@dataclass
class Chain:
    """Snapshot of the tracked option chain for one underlying + expiry."""

    underlying: str
    spot: float
    atm: float
    expiry: date
    strike_step: float
    rows: Dict[float, Dict[str, ChainRow]] = field(default_factory=dict)
    taken_at: datetime = field(default_factory=clock.now)

    def get(self, strike: float, opt_type: OptionType) -> Optional[ChainRow]:
        return self.rows.get(float(strike), {}).get(str(opt_type))

    def strikes(self) -> List[float]:
        return sorted(self.rows.keys())

    @property
    def dte(self) -> int:
        return clock.dte(self.expiry)

    @property
    def is_expiry_day(self) -> bool:
        return self.dte == 0


# ══════════════════════════════════════════════════════════════════════════════
#  Orders / legs / positions
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class LegSpec:
    """What a strategy asks for, before anything is executed."""

    tradingsymbol: str
    token: int
    exchange: str
    strike: float
    opt_type: OptionType
    side: Side
    lots: int
    lot_size: int
    role: LegRole
    tick_size: float = 0.05
    ref_price: float = 0.0               # price used when the plan was built

    @property
    def quantity(self) -> int:
        return self.lots * self.lot_size


@dataclass
class Leg:
    """A live leg of an open position."""

    tradingsymbol: str
    token: int
    exchange: str
    strike: float
    opt_type: OptionType
    side: Side
    lots: int
    lot_size: int
    role: LegRole
    tick_size: float = 0.05

    entry_price: float = 0.0
    exit_price: float = 0.0
    mark: float = 0.0
    filled_qty: int = 0
    state: LegState = LegState.PENDING
    order_id: Optional[str] = None
    exit_order_id: Optional[str] = None
    protective_sl_order_id: Optional[str] = None
    entry_time: Optional[datetime] = None
    exit_time: Optional[datetime] = None
    delta: float = 0.0
    gamma: float = 0.0
    theta: float = 0.0
    vega: float = 0.0
    iv: float = 0.0
    costs: float = 0.0
    adjustment_seq: int = 0              # 0 = original leg
    note: str = ""

    # ── Basics ────────────────────────────────────────────────────────────────
    @property
    def quantity(self) -> int:
        # filled_qty is what the broker actually holds. After a partial exit it
        # is the remainder, so the next close does not repeat size already filled.
        if self.filled_qty > 0:
            return self.filled_qty
        return self.lots * self.lot_size

    @property
    def is_short(self) -> bool:
        return self.side is Side.SELL

    @property
    def is_open(self) -> bool:
        return self.state in (LegState.OPEN, LegState.CLOSING)

    @property
    def signed_qty(self) -> int:
        return self.quantity * self.side.sign

    @property
    def entry_credit(self) -> float:
        """Rupees received (positive) or paid (negative) at entry."""
        return -self.side.sign * self.entry_price * self.quantity

    # ── PnL ───────────────────────────────────────────────────────────────────
    def unrealised_pnl(self, mark: Optional[float] = None) -> float:
        m = self.mark if mark is None else mark
        if m <= 0 or self.entry_price <= 0:
            return 0.0
        if self.is_short:
            return (self.entry_price - m) * self.quantity
        return (m - self.entry_price) * self.quantity

    def realised_pnl(self) -> float:
        if self.state is not LegState.CLOSED or self.exit_price <= 0:
            return 0.0
        if self.is_short:
            return (self.entry_price - self.exit_price) * self.quantity
        return (self.exit_price - self.entry_price) * self.quantity

    def pnl(self) -> float:
        return self.realised_pnl() if self.state is LegState.CLOSED else self.unrealised_pnl()

    @property
    def premium_multiple(self) -> float:
        """How many times the entry premium the leg now trades at (shorts: >1 = losing)."""
        if self.entry_price <= 0 or self.mark <= 0:
            return 1.0
        return self.mark / self.entry_price

    @property
    def decay_pct(self) -> float:
        """Fraction of a short leg's premium that has decayed (0..1)."""
        if self.entry_price <= 0 or self.mark <= 0:
            return 0.0
        return max(0.0, min(1.0, 1.0 - self.mark / self.entry_price))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tradingsymbol": self.tradingsymbol,
            "token": self.token,
            "exchange": self.exchange,
            "strike": self.strike,
            "opt_type": str(self.opt_type),
            "side": str(self.side),
            "lots": self.lots,
            "lot_size": self.lot_size,
            "role": str(self.role),
            "tick_size": self.tick_size,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "mark": self.mark,
            "filled_qty": self.filled_qty,
            "state": str(self.state),
            "order_id": self.order_id,
            "exit_order_id": self.exit_order_id,
            "protective_sl_order_id": self.protective_sl_order_id,
            "entry_time": self.entry_time.isoformat() if self.entry_time else None,
            "exit_time": self.exit_time.isoformat() if self.exit_time else None,
            "delta": self.delta,
            "iv": self.iv,
            "costs": self.costs,
            "adjustment_seq": self.adjustment_seq,
            "note": self.note,
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Leg":
        def _dt(v):
            return datetime.fromisoformat(v) if v else None

        return Leg(
            tradingsymbol=d["tradingsymbol"],
            token=int(d["token"]),
            exchange=d["exchange"],
            strike=float(d["strike"]),
            opt_type=OptionType(d["opt_type"]),
            side=Side(d["side"]),
            lots=int(d["lots"]),
            lot_size=int(d["lot_size"]),
            role=LegRole(d["role"]),
            tick_size=float(d.get("tick_size", 0.05)),
            entry_price=float(d.get("entry_price", 0.0)),
            exit_price=float(d.get("exit_price", 0.0)),
            mark=float(d.get("mark", 0.0)),
            filled_qty=int(d.get("filled_qty", 0)),
            state=LegState(d.get("state", "PENDING")),
            order_id=d.get("order_id"),
            exit_order_id=d.get("exit_order_id"),
            protective_sl_order_id=d.get("protective_sl_order_id"),
            entry_time=_dt(d.get("entry_time")),
            exit_time=_dt(d.get("exit_time")),
            delta=float(d.get("delta", 0.0)),
            iv=float(d.get("iv", 0.0)),
            costs=float(d.get("costs", 0.0)),
            adjustment_seq=int(d.get("adjustment_seq", 0)),
            note=d.get("note", ""),
        )


@dataclass
class AdjustmentRecord:
    action: AdjustAction
    at: datetime
    reason: str
    detail: str = ""
    realised_delta: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": str(self.action),
            "at": self.at.isoformat(),
            "reason": self.reason,
            "detail": self.detail,
            "realised_delta": self.realised_delta,
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "AdjustmentRecord":
        return AdjustmentRecord(
            action=AdjustAction(d["action"]),
            at=datetime.fromisoformat(d["at"]),
            reason=d.get("reason", ""),
            detail=d.get("detail", ""),
            realised_delta=float(d.get("realised_delta", 0.0)),
        )


@dataclass
class Position:
    """A multi-leg option-selling position managed as one risk unit."""

    id: str
    underlying: str
    strategy: str
    expiry: date
    legs: List[Leg] = field(default_factory=list)
    state: PositionState = PositionState.PENDING_ENTRY
    lots: int = 0
    lot_size: int = 0

    entry_time: Optional[datetime] = None
    exit_time: Optional[datetime] = None
    entry_spot: float = 0.0
    exit_spot: float = 0.0

    credit: float = 0.0                 # rupees received at entry (net, all legs)
    target_pnl: float = 0.0             # rupees
    stop_pnl: float = 0.0               # rupees (negative)
    max_loss_estimate: float = 0.0      # rupees (theoretical, defined-risk only)
    margin_blocked: float = 0.0

    peak_pnl: float = 0.0
    trough_pnl: float = 0.0
    trail_stop_pnl: Optional[float] = None
    trail_armed: bool = False

    realised_pnl: float = 0.0
    costs: float = 0.0
    exit_reason: Optional[ExitReason] = None
    adjustments: List[AdjustmentRecord] = field(default_factory=list)

    entry_view: Dict[str, Any] = field(default_factory=dict)
    entry_score: float = 0.0
    entry_reasons: List[str] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)

    # ── Leg access ────────────────────────────────────────────────────────────
    @property
    def open_legs(self) -> List[Leg]:
        return [l for l in self.legs if l.is_open]

    @property
    def short_legs(self) -> List[Leg]:
        return [l for l in self.open_legs if l.is_short]

    @property
    def long_legs(self) -> List[Leg]:
        return [l for l in self.open_legs if not l.is_short]

    def legs_of(self, opt_type: OptionType, short_only: bool = False) -> List[Leg]:
        out = [l for l in self.open_legs if l.opt_type is opt_type]
        return [l for l in out if l.is_short] if short_only else out

    @property
    def is_active(self) -> bool:
        return self.state in (
            PositionState.PENDING_ENTRY,
            PositionState.OPEN,
            PositionState.ADJUSTING,
            PositionState.PENDING_EXIT,
        )

    @property
    def is_defined_risk(self) -> bool:
        """True when every short leg is covered by a long leg on the same side."""
        for opt_type in (OptionType.CE, OptionType.PE):
            shorts = sum(l.quantity for l in self.open_legs
                         if l.opt_type is opt_type and l.is_short)
            longs = sum(l.quantity for l in self.open_legs
                        if l.opt_type is opt_type and not l.is_short)
            if shorts > longs:
                return False
        return True

    # ── PnL / greeks ──────────────────────────────────────────────────────────
    def unrealised_pnl(self) -> float:
        return sum(l.unrealised_pnl() for l in self.open_legs)

    def total_pnl(self) -> float:
        """
        Booked slices + closed legs + open mark-to-market - costs.

        A partial exit stores its rupees on `realised_pnl` and shrinks the leg.
        A finished leg keeps its own rupees. The two are not the same contracts,
        so both belong in the total.
        """
        closed = sum(l.realised_pnl() for l in self.legs if l.state is LegState.CLOSED)
        return self.realised_pnl + closed + self.unrealised_pnl() - self.costs

    def pnl_pct_of_credit(self) -> float:
        if self.credit <= 0:
            return 0.0
        return self.total_pnl() / self.credit

    def age_minutes(self) -> float:
        if not self.entry_time:
            return 0.0
        return (clock.now() - self.entry_time).total_seconds() / 60.0

    @property
    def short_strikes(self) -> Dict[str, float]:
        out: Dict[str, float] = {}
        for leg in self.short_legs:
            key = str(leg.opt_type)
            if key not in out:
                out[key] = leg.strike
            else:
                # Keep the strike closest to danger (lowest CE, highest PE).
                if leg.opt_type is OptionType.CE:
                    out[key] = min(out[key], leg.strike)
                else:
                    out[key] = max(out[key], leg.strike)
        return out

    def describe(self) -> str:
        parts = []
        for leg in sorted(self.legs, key=lambda l: (str(l.opt_type), l.strike)):
            if leg.state in (LegState.REJECTED,):
                continue
            sign = "-" if leg.is_short else "+"
            closed = "x" if leg.state is LegState.CLOSED else ""
            parts.append(f"{sign}{leg.lots}x{int(leg.strike)}{leg.opt_type}{closed}")
        return " ".join(parts)

    # ── Persistence ───────────────────────────────────────────────────────────
    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "underlying": self.underlying,
            "strategy": self.strategy,
            "expiry": self.expiry.isoformat() if self.expiry else None,
            "legs": [l.to_dict() for l in self.legs],
            "state": str(self.state),
            "lots": self.lots,
            "lot_size": self.lot_size,
            "entry_time": self.entry_time.isoformat() if self.entry_time else None,
            "exit_time": self.exit_time.isoformat() if self.exit_time else None,
            "entry_spot": self.entry_spot,
            "exit_spot": self.exit_spot,
            "credit": self.credit,
            "target_pnl": self.target_pnl,
            "stop_pnl": self.stop_pnl,
            "max_loss_estimate": self.max_loss_estimate,
            "margin_blocked": self.margin_blocked,
            "peak_pnl": self.peak_pnl,
            "trough_pnl": self.trough_pnl,
            "trail_stop_pnl": self.trail_stop_pnl,
            "trail_armed": self.trail_armed,
            "realised_pnl": self.realised_pnl,
            "costs": self.costs,
            "exit_reason": str(self.exit_reason) if self.exit_reason else None,
            "adjustments": [a.to_dict() for a in self.adjustments],
            "entry_view": self.entry_view,
            "entry_score": self.entry_score,
            "entry_reasons": self.entry_reasons,
            "meta": self.meta,
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Position":
        def _dt(v):
            return datetime.fromisoformat(v) if v else None

        return Position(
            id=d["id"],
            underlying=d["underlying"],
            strategy=d["strategy"],
            expiry=clock.as_date(d.get("expiry")),
            legs=[Leg.from_dict(x) for x in d.get("legs", [])],
            state=PositionState(d.get("state", "OPEN")),
            lots=int(d.get("lots", 0)),
            lot_size=int(d.get("lot_size", 0)),
            entry_time=_dt(d.get("entry_time")),
            exit_time=_dt(d.get("exit_time")),
            entry_spot=float(d.get("entry_spot", 0.0)),
            exit_spot=float(d.get("exit_spot", 0.0)),
            credit=float(d.get("credit", 0.0)),
            target_pnl=float(d.get("target_pnl", 0.0)),
            stop_pnl=float(d.get("stop_pnl", 0.0)),
            max_loss_estimate=float(d.get("max_loss_estimate", 0.0)),
            margin_blocked=float(d.get("margin_blocked", 0.0)),
            peak_pnl=float(d.get("peak_pnl", 0.0)),
            trough_pnl=float(d.get("trough_pnl", 0.0)),
            trail_stop_pnl=d.get("trail_stop_pnl"),
            trail_armed=bool(d.get("trail_armed", False)),
            realised_pnl=float(d.get("realised_pnl", 0.0)),
            costs=float(d.get("costs", 0.0)),
            exit_reason=ExitReason(d["exit_reason"]) if d.get("exit_reason") else None,
            adjustments=[AdjustmentRecord.from_dict(x) for x in d.get("adjustments", [])],
            entry_view=d.get("entry_view", {}),
            entry_score=float(d.get("entry_score", 0.0)),
            entry_reasons=d.get("entry_reasons", []),
            meta=d.get("meta", {}),
        )


# ══════════════════════════════════════════════════════════════════════════════
#  Strategy plan
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class StrategyPlan:
    """A fully specified, ready-to-execute trade proposal."""

    strategy: str
    underlying: str
    expiry: date
    legs: List[LegSpec]
    lots: int
    lot_size: int
    credit_per_unit: float = 0.0        # rupees per share of net credit
    score: float = 0.0
    reasons: List[str] = field(default_factory=list)
    max_loss_per_unit: float = 0.0      # theoretical, per share (0 = undefined)
    target_pct: float = 0.0
    sl_pct: float = 0.0
    margin_estimate: float = 0.0
    meta: Dict[str, Any] = field(default_factory=dict)

    @property
    def credit_rupees(self) -> float:
        return self.credit_per_unit * self.lots * self.lot_size

    @property
    def max_loss_rupees(self) -> float:
        return self.max_loss_per_unit * self.lots * self.lot_size

    @property
    def is_defined_risk(self) -> bool:
        return self.max_loss_per_unit > 0

    def describe(self) -> str:
        parts = []
        for leg in self.legs:
            sign = "-" if leg.side is Side.SELL else "+"
            parts.append(f"{sign}{leg.lots}x{int(leg.strike)}{leg.opt_type}")
        return " ".join(parts)


@dataclass
class OrderResult:
    ok: bool
    order_id: Optional[str] = None
    filled_qty: int = 0
    avg_price: float = 0.0
    message: str = ""
    requested_qty: int = 0

    @property
    def fully_filled(self) -> bool:
        return self.ok and self.requested_qty > 0 and self.filled_qty >= self.requested_qty
