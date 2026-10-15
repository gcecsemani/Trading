"""
execution/order_router.py
=========================
Turns a StrategyPlan into a live Position, and guarantees that exits complete.

Entry rules
-----------
* Protective LONG legs are bought **first**, short legs sold after. The position
  is therefore never momentarily naked, and the margin benefit of the hedge is
  already recognised when the short order hits.
* Orders start as a limit slightly better than the touch, then step one tick more
  aggressive per attempt, then cross, then fall back to MARKET.
* Quantities above the exchange freeze limit are sliced automatically.
* If any leg cannot be filled, everything already filled is reversed and the
  trade is abandoned. A half-built spread is a worse position than no position.
* Unequal fills are trimmed back to a balanced structure before the position is
  accepted, so a 4-leg condor can never end up as a 3-leg naked short.

Exit rules
----------
* SHORT legs are bought back **first** — that is what actually removes the risk.
  The long hedges are sold afterwards.
* Exits escalate faster than entries and always end in a MARKET order if the
  limit chase fails. `exit_position` is idempotent: it only touches legs that
  are still open, so the monitor loop can simply call it again on the next tick
  until it reports success.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

import config
from core import clock
from core.enums import (
    ExitReason,
    LegState,
    OrderStatus,
    PositionState,
    Side,
)
from core.logging_setup import get
from core.models import Leg, LegSpec, Position, StrategyPlan, new_id
from data.tick_store import TickStore
from execution.broker_base import Broker, OrderRequest, OrderSnapshot, round_to_tick

log = get("router")


@dataclass
class ExecutionReport:
    ok: bool
    filled_qty: int = 0
    avg_price: float = 0.0
    orders: int = 0
    message: str = ""


class OrderRouter:
    def __init__(
        self,
        broker: Broker,
        ticks: TickStore,
        on_order_failure: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.broker = broker
        self.ticks = ticks
        self._on_failure = on_order_failure
        self.order_failures = 0

    # ══════════════════════════════════════════════════════════════════════════
    #  Low-level execution
    # ══════════════════════════════════════════════════════════════════════════

    def _limit_price(
        self,
        token: int,
        side: Side,
        tick_size: float,
        ticks_inside: int,
        attempt: int,
        quantity: int,
    ) -> float:
        """
        Price the order off the depth level that covers `quantity`.

        A buy's limit is the ask you must lift. A sell's limit is the bid you
        must hit. Later attempts give up one more tick so a thin top-of-book
        does not leave the order sitting.
        """
        tick = self.ticks.get(token)
        if tick is None or tick.ltp <= 0:
            return 0.0

        cover = tick.cover_price(side, quantity)
        if cover <= 0:
            cover = tick.ask if side is Side.BUY else tick.bid
        if cover <= 0:
            cover = tick.ltp

        concession = attempt * tick_size
        if side is Side.BUY:
            price = cover - ticks_inside * tick_size + concession
        else:
            price = cover + ticks_inside * tick_size - concession
        return round_to_tick(max(tick_size, price), tick_size, side)

    def _slices(self, quantity: int, symbol: str, lot_size: int = 0) -> List[int]:
        """
        Split an order so every slice is a whole number of lots and none of
        them exceeds the exchange freeze quantity. NIFTY's freeze is 1800 and
        its lot is 65, so the largest legal slice is 1755 (27 lots), not 1800.
        """
        cfg = config.symbol_cfg(symbol)
        freeze = int(cfg.get("freeze_qty") or 0)
        lot = int(lot_size or cfg.get("lot_size") or 1)
        if lot <= 0:
            lot = 1
        cap = freeze
        if freeze > 0:
            cap = (freeze // lot) * lot or lot
        if not config.EXECUTION["slice_above_freeze"] or freeze <= 0 or quantity <= cap:
            return [quantity]
        out = []
        remaining = quantity
        while remaining > 0:
            chunk = min(cap, remaining)
            if chunk >= lot:
                chunk = (chunk // lot) * lot
            if chunk <= 0:
                chunk = remaining
            out.append(chunk)
            remaining -= chunk
        log.info("Slicing %d into %s (freeze %d, lot %d, cap %d)",
                 quantity, out, freeze, lot, cap)
        return out

    def _execute_slice(
        self,
        symbol: str,
        token: int,
        tradingsymbol: str,
        exchange: str,
        side: Side,
        quantity: int,
        tick_size: float,
        is_exit: bool,
        tag: str,
    ) -> ExecutionReport:
        cfg = config.EXECUTION
        ticks_inside = cfg["exit_limit_ticks"] if is_exit else cfg["entry_limit_ticks"]
        max_attempts = cfg["exit_modify_attempts"] if is_exit else cfg["modify_attempts"]
        timeout = cfg["exit_fill_timeout_sec"] if is_exit else cfg["fill_timeout_sec"]
        allow_market = cfg["exit_market_fallback"] if is_exit else cfg["market_fallback"]
        poll = cfg["fill_poll_sec"]

        filled = 0
        notional = 0.0
        orders_placed = 0

        def absorb(snapshot: OrderSnapshot) -> None:
            """Average price on a snapshot is cumulative for that order, not incremental."""
            nonlocal filled, notional
            if snapshot.filled_qty > filled:
                filled = snapshot.filled_qty
                if snapshot.avg_price > 0:
                    notional = snapshot.avg_price * filled

        price = self._limit_price(token, side, tick_size, ticks_inside, 0, quantity)
        if price <= 0:
            # No book at all — a limit price cannot be formed. Go straight to
            # market for exits; refuse for entries.
            if not (is_exit and allow_market):
                return ExecutionReport(False, message="no market data for limit price")
            price = 0.0
            order_type = "MARKET"
        else:
            order_type = "LIMIT"

        request = OrderRequest(
            exchange=exchange,
            tradingsymbol=tradingsymbol,
            token=token,
            side=side,
            quantity=quantity,
            order_type=order_type,
            price=price,
            tick_size=tick_size,
            tag=tag,
        )
        order_id = self.broker.place(request)
        orders_placed += 1
        if not order_id:
            self._note_failure(f"place failed: {side} {tradingsymbol} x{quantity}")
            return ExecutionReport(False, orders=orders_placed, message="order rejected")

        attempt = 0
        while True:
            deadline = time.monotonic() + timeout
            snapshot = self.broker.status(order_id)
            while not snapshot.is_final and time.monotonic() < deadline:
                time.sleep(poll)
                snapshot = self.broker.status(order_id)

            absorb(snapshot)
            # The websocket can stay OPEN after the exchange has already filled.
            # Confirm before chasing or sending a second order for the same size.
            snapshot = self._confirmed_status(order_id)
            absorb(snapshot)

            if snapshot.status is OrderStatus.COMPLETE or (filled >= quantity and filled > 0):
                if filled <= 0:
                    self._note_failure(
                        f"complete with no fill quantity: {side} {tradingsymbol}"
                    )
                    return ExecutionReport(
                        False, 0, 0.0, orders_placed, "complete but unfilled",
                    )
                avg = notional / filled if filled else snapshot.avg_price
                return ExecutionReport(True, filled, round(avg, 2), orders_placed, "filled")

            if snapshot.status is OrderStatus.REJECTED:
                self._note_failure(
                    f"rejected: {side} {tradingsymbol} x{quantity} — {snapshot.message}"
                )
                break

            attempt += 1
            if attempt <= max_attempts and snapshot.status is OrderStatus.OPEN:
                new_price = self._limit_price(
                    token, side, tick_size, ticks_inside, attempt, quantity
                )
                if new_price > 0:
                    log.info("chase %s attempt %d -> %.2f (%s %s)",
                             order_id, attempt, new_price, side, tradingsymbol)
                    if self.broker.modify(order_id, new_price, tick_size, side,
                                          critical=is_exit):
                        continue
                # Modify failed: cancel and fall through to the market fallback.
            self.broker.cancel(order_id)
            absorb(self._confirmed_status(order_id))
            break

        remaining = quantity - filled
        if remaining <= 0:
            avg = notional / filled if filled else 0.0
            return ExecutionReport(True, filled, round(avg, 2), orders_placed, "filled")

        if not allow_market:
            avg = notional / filled if filled else 0.0
            return ExecutionReport(
                filled > 0, filled, round(avg, 2), orders_placed,
                f"limit chase exhausted, {remaining} unfilled",
            )

        log.warning("MARKET fallback: %s %s x%d (%s)",
                    side, tradingsymbol, remaining, "exit" if is_exit else "entry")
        market_request = OrderRequest(
            exchange=exchange,
            tradingsymbol=tradingsymbol,
            token=token,
            side=side,
            quantity=remaining,
            order_type="MARKET",
            tick_size=tick_size,
            tag=tag,
        )
        market_id = self.broker.place(market_request)
        orders_placed += 1
        if not market_id:
            self._note_failure(f"market fallback rejected: {side} {tradingsymbol}")
            avg = notional / filled if filled else 0.0
            return ExecutionReport(filled > 0, filled, round(avg, 2), orders_placed,
                                   "market fallback rejected")

        deadline = time.monotonic() + max(timeout, 5.0)
        snapshot = self.broker.status(market_id)
        while not snapshot.is_final and time.monotonic() < deadline:
            time.sleep(poll)
            snapshot = self.broker.status(market_id)

        snapshot = self._confirmed_status(market_id)
        if snapshot.filled_qty > 0:
            if snapshot.avg_price > 0:
                notional += snapshot.avg_price * snapshot.filled_qty
            filled += snapshot.filled_qty

        avg = notional / filled if filled else 0.0
        ok = filled >= quantity
        if not ok:
            self._note_failure(
                f"INCOMPLETE {'exit' if is_exit else 'entry'}: {side} {tradingsymbol} "
                f"{filled}/{quantity}"
            )
        return ExecutionReport(ok, filled, round(avg, 2), orders_placed,
                               "filled" if ok else "partial after market fallback")

    def _execute(
        self,
        symbol: str,
        token: int,
        tradingsymbol: str,
        exchange: str,
        side: Side,
        quantity: int,
        tick_size: float,
        is_exit: bool,
        tag: str,
        lot_size: int = 0,
    ) -> ExecutionReport:
        """Executes a full quantity, slicing above the exchange freeze limit."""
        total_filled = 0
        notional = 0.0
        orders = 0
        ok = True
        message = ""

        for chunk in self._slices(quantity, symbol, lot_size):
            report = self._execute_slice(
                symbol, token, tradingsymbol, exchange, side, chunk,
                tick_size, is_exit, tag,
            )
            orders += report.orders
            if report.filled_qty > 0:
                notional += report.avg_price * report.filled_qty
                total_filled += report.filled_qty
            if not report.ok:
                ok = False
                message = report.message
                if not is_exit:
                    break       # stop adding risk when an entry slice fails
            if len(self._slices(quantity, symbol)) > 1:
                time.sleep(config.EXECUTION["slice_sleep_sec"])

        avg = notional / total_filled if total_filled else 0.0
        return ExecutionReport(ok, total_filled, round(avg, 2), orders,
                               message or ("filled" if ok else "incomplete"))

    def _confirmed_status(self, order_id: str) -> OrderSnapshot:
        """Websocket status, replaced when order history shows a later fill."""
        snapshot = self.broker.status(order_id)
        rest = getattr(self.broker, "status_rest", None)
        if rest is None or snapshot.is_complete:
            return snapshot
        try:
            history = rest(order_id)
        except Exception:
            log.exception("order history lookup failed for %s", order_id)
            return snapshot
        from execution.live_broker import prefer_snapshot
        return prefer_snapshot(snapshot, history)

    def _note_failure(self, message: str) -> None:
        self.order_failures += 1
        log.error("ORDER FAILURE (%d): %s", self.order_failures, message)
        if self._on_failure:
            try:
                self._on_failure(message)
            except Exception:                              # pragma: no cover
                log.debug("failure callback raised", exc_info=True)

    # ══════════════════════════════════════════════════════════════════════════
    #  Entry
    # ══════════════════════════════════════════════════════════════════════════

    def _ordered_specs(self, specs: List[LegSpec]) -> List[LegSpec]:
        if config.EXECUTION["hedge_first_on_entry"]:
            return sorted(specs, key=lambda s: 0 if s.side is Side.BUY else 1)
        return list(specs)

    def enter(self, plan: StrategyPlan, spot: float) -> Optional[Position]:
        position = Position(
            id=new_id("POS"),
            underlying=plan.underlying,
            strategy=plan.strategy,
            expiry=plan.expiry,
            state=PositionState.PENDING_ENTRY,
            lots=plan.lots,
            lot_size=plan.lot_size,
            entry_spot=spot,
            entry_score=plan.score,
            entry_reasons=list(plan.reasons),
            max_loss_estimate=plan.max_loss_rupees,
            margin_blocked=plan.margin_estimate,
            meta=dict(plan.meta),
        )

        tag = f"optsell-{plan.strategy[:8]}"
        filled_legs: List[Leg] = []

        for spec in self._ordered_specs(plan.legs):
            report = self._execute(
                plan.underlying, spec.token, spec.tradingsymbol, spec.exchange,
                spec.side, spec.quantity, spec.tick_size, is_exit=False, tag=tag,
                lot_size=spec.lot_size,
            )
            position.costs += report.orders * self.broker.costs_per_order()

            if report.filled_qty <= 0:
                log.error("%s: leg %s %s failed (%s) — rolling back",
                          plan.strategy, spec.side, spec.tradingsymbol, report.message)
                self._rollback(plan.underlying, filled_legs, tag)
                position.state = PositionState.FAILED
                return None

            leg = Leg(
                tradingsymbol=spec.tradingsymbol,
                token=spec.token,
                exchange=spec.exchange,
                strike=spec.strike,
                opt_type=spec.opt_type,
                side=spec.side,
                lots=max(1, report.filled_qty // spec.lot_size),
                lot_size=spec.lot_size,
                role=spec.role,
                tick_size=spec.tick_size,
                entry_price=report.avg_price,
                mark=report.avg_price,
                filled_qty=report.filled_qty,
                state=LegState.OPEN,
                entry_time=clock.now(),
            )
            filled_legs.append(leg)

            if not report.ok:
                log.warning("%s: leg %s partially filled %d/%d",
                            plan.strategy, spec.tradingsymbol,
                            report.filled_qty, spec.quantity)

        position.legs = filled_legs
        if not self._balance(position, plan, tag):
            self._rollback(plan.underlying, position.open_legs, tag)
            position.state = PositionState.FAILED
            return None

        position.state = PositionState.OPEN
        position.entry_time = clock.now()
        position.credit = sum(l.entry_credit for l in position.open_legs)

        if position.credit <= 0:
            log.error("%s: filled as a net debit (%.0f) — unwinding immediately",
                      plan.strategy, position.credit)
            self._rollback(plan.underlying, position.open_legs, tag)
            position.state = PositionState.FAILED
            return None

        self._set_targets(position, plan)
        return position

    def _balance(self, position: Position, plan: StrategyPlan, tag: str) -> bool:
        """
        Trim unequal fills back to the plan's leg ratios so the risk shape is
        exactly what was approved. Returns False when nothing usable remains.
        """
        ratios: Dict[Tuple[float, str, str], int] = {}
        base_lots = min((s.lots for s in plan.legs), default=0)
        if base_lots <= 0:
            return False
        for spec in plan.legs:
            key = (spec.strike, str(spec.opt_type), str(spec.side))
            ratios[key] = max(1, spec.lots // base_lots)

        achieved = []
        for leg in position.open_legs:
            key = (leg.strike, str(leg.opt_type), str(leg.side))
            ratio = ratios.get(key, 1)
            achieved.append(leg.lots // ratio)
        target_base = min(achieved) if achieved else 0

        if target_base <= 0:
            log.error("%s: no complete unit filled — abandoning", plan.strategy)
            return False

        for leg in list(position.open_legs):
            key = (leg.strike, str(leg.opt_type), str(leg.side))
            ratio = ratios.get(key, 1)
            wanted = target_base * ratio
            excess_lots = leg.lots - wanted
            if excess_lots <= 0:
                continue
            excess_qty = excess_lots * leg.lot_size
            log.warning("%s: trimming %d excess lots from %s",
                        plan.strategy, excess_lots, leg.tradingsymbol)
            report = self._execute(
                position.underlying, leg.token, leg.tradingsymbol, leg.exchange,
                leg.side.opposite, excess_qty, leg.tick_size, is_exit=True, tag=tag,
                lot_size=leg.lot_size,
            )
            position.costs += report.orders * self.broker.costs_per_order()
            if report.filled_qty > 0:
                closed_lots = report.filled_qty // leg.lot_size
                leg.lots -= closed_lots
                leg.filled_qty -= report.filled_qty
                # The trimmed quantity is realised immediately at the trim price.
                if leg.is_short:
                    position.realised_pnl += (leg.entry_price - report.avg_price) * report.filled_qty
                else:
                    position.realised_pnl += (report.avg_price - leg.entry_price) * report.filled_qty

        position.lots = target_base
        if target_base != plan.lots:
            log.warning("%s: sized down to %d lots (planned %d)",
                        plan.strategy, target_base, plan.lots)
        return True

    def _set_targets(self, position: Position, plan: StrategyPlan) -> None:
        """
        The stop and the target live on the sold premium, not on a percent of credit.

        Sold at 120 with a 50-point stop and a 10-point target means: buy it
        back if it trades at 170, or when it trades at 10. The hedge is not
        given a stop.
        """
        short = next((leg for leg in position.short_legs), None)
        position.target_pnl = 0.0
        position.stop_pnl = 0.0
        if short is None or short.entry_price <= 0:
            return
        sl = round(short.entry_price + float(config.TRADE["sl_points"]), 2)
        target = float(config.TRADE["target_premium"])
        position.meta["short_entry"] = short.entry_price
        position.meta["sl_premium"] = sl
        position.meta["target_premium"] = target
        position.meta["short_symbol"] = short.tradingsymbol
        hedge = next((leg for leg in position.long_legs), None)
        if hedge is not None:
            position.meta["hedge_entry"] = hedge.entry_price
            position.meta["hedge_symbol"] = hedge.tradingsymbol
        log.info(
            "ENTERED %s %s %s | short %.2f | SL %.2f | target ≤ %.2f | hedge has no SL",
            position.id, position.strategy, position.describe(),
            short.entry_price, sl, target,
        )

    def _rollback(self, symbol: str, legs: List[Leg], tag: str) -> None:
        """Reverse everything that filled during a failed entry."""
        if not legs:
            return
        if not config.EXECUTION["rollback_on_partial_entry"]:
            log.critical("Partial entry left open (rollback disabled): %s",
                         [l.tradingsymbol for l in legs])
            return

        log.warning("Rolling back %d filled leg(s)", len(legs))
        # Close shorts first — that is the risk.
        for leg in sorted(legs, key=lambda l: 0 if l.is_short else 1):
            if leg.state is not LegState.OPEN:
                continue
            report = self._execute(
                symbol, leg.token, leg.tradingsymbol, leg.exchange,
                leg.side.opposite, leg.quantity, leg.tick_size, is_exit=True, tag=tag,
                lot_size=leg.lot_size,
            )
            if report.filled_qty >= leg.quantity:
                leg.state = LegState.CLOSED
                leg.exit_price = report.avg_price
                leg.exit_time = clock.now()
            else:
                self._note_failure(
                    f"ROLLBACK INCOMPLETE for {leg.tradingsymbol} "
                    f"({report.filled_qty}/{leg.quantity}) — MANUAL CHECK REQUIRED"
                )

    # ══════════════════════════════════════════════════════════════════════════
    #  Exit
    # ══════════════════════════════════════════════════════════════════════════

    def close_legs(
        self,
        position: Position,
        legs: List[Leg],
        tag_suffix: str = "exit",
    ) -> bool:
        """
        Close specific legs. Shorts are bought back before longs are sold, so risk
        comes off first. Safe to call repeatedly — closed legs are skipped.
        """
        tag = f"optsell-{tag_suffix[:10]}"
        order = sorted(
            legs,
            key=lambda l: 0 if l.is_short else 1,
        ) if config.EXECUTION["shorts_first_on_exit"] else list(legs)

        all_done = True
        for leg in order:
            if leg.state in (LegState.CLOSED, LegState.REJECTED):
                continue
            leg.state = LegState.CLOSING
            self._cancel_protective(leg)

            report = self._execute(
                position.underlying, leg.token, leg.tradingsymbol, leg.exchange,
                leg.side.opposite, leg.quantity, leg.tick_size, is_exit=True, tag=tag,
                lot_size=leg.lot_size,
            )
            position.costs += report.orders * self.broker.costs_per_order()

            if report.filled_qty >= leg.quantity:
                leg.exit_price = report.avg_price
                leg.exit_time = clock.now()
                leg.state = LegState.CLOSED
                log.info("closed %s %s x%d @ %.2f",
                         leg.side, leg.tradingsymbol, leg.quantity, report.avg_price)
            elif report.filled_qty > 0 and report.avg_price <= 0:
                leg.state = LegState.OPEN
                all_done = False
                log.error("PARTIAL exit on %s has no price — will retry", leg.tradingsymbol)
            elif report.filled_qty > 0:
                # Book the contracts that actually filled, then leave only the
                # remainder open. The next pass orders that remainder, not the
                # original size.
                qty = min(report.filled_qty, leg.quantity)
                if leg.is_short:
                    position.realised_pnl += (leg.entry_price - report.avg_price) * qty
                else:
                    position.realised_pnl += (report.avg_price - leg.entry_price) * qty
                remaining = leg.quantity - qty
                if leg.lot_size > 0:
                    tradable = (remaining // leg.lot_size) * leg.lot_size
                    odd = remaining - tradable
                    if odd:
                        log.critical(
                            "%s exit left %d contracts that are not a whole lot",
                            leg.tradingsymbol, odd,
                        )
                    remaining = tradable
                leg.filled_qty = remaining
                leg.lots = remaining // leg.lot_size if leg.lot_size else 0
                if remaining <= 0:
                    # The rupees are already on position.realised_pnl. Keep the
                    # leg from adding them a second time.
                    leg.exit_price = report.avg_price
                    leg.exit_time = clock.now()
                    leg.state = LegState.CLOSED
                    leg.filled_qty = 0
                    leg.lots = 0
                    log.error(
                        "PARTIAL exit on %s filled %d and left an odd remainder in the book",
                        leg.tradingsymbol, qty,
                    )
                else:
                    leg.state = LegState.OPEN
                    all_done = False
                    log.error("PARTIAL exit on %s (%d filled, %d still open) — will retry",
                              leg.tradingsymbol, qty, remaining)
            else:
                leg.state = LegState.OPEN
                all_done = False
                log.error("EXIT FAILED on %s (%s) — will retry",
                          leg.tradingsymbol, report.message)

        return all_done

    def exit_position(self, position: Position, reason: ExitReason) -> bool:
        """
        Flatten the whole position. Idempotent: call again next loop if it
        returns False.
        """
        position.state = PositionState.PENDING_EXIT
        if position.exit_reason is None:
            position.exit_reason = reason

        done = self.close_legs(position, list(position.open_legs),
                               tag_suffix=str(reason)[:10])
        if not done:
            log.error("%s: exit incomplete (%s) — retrying on the next cycle",
                      position.id, reason)
            return False

        position.state = PositionState.CLOSED
        position.exit_time = clock.now()
        # Closed-leg rupees stay on the legs. Partial slices already sit on
        # position.realised_pnl. total_pnl adds each once.
        log.info("EXITED %s %s reason=%s pnl=%.0f",
                 position.id, position.strategy, reason, position.total_pnl())
        return True

    # ══════════════════════════════════════════════════════════════════════════
    #  Protective (disaster) stop orders
    # ══════════════════════════════════════════════════════════════════════════

    def place_protective_stops(self, position: Position) -> int:
        """
        Resting broker-side stops on every short leg. The bot's own (tighter)
        stop normally fires first; these only matter if the process, network or
        machine dies while a short position is open.
        """
        cfg = config.EXECUTION["protective_sl"]
        if not cfg["enabled"]:
            return 0

        placed = 0
        for leg in position.short_legs:
            if leg.protective_sl_order_id or leg.entry_price <= 0:
                continue
            trigger = round_to_tick(
                leg.entry_price + float(config.TRADE["sl_points"]) + float(cfg.get("extra_points") or 0),
                leg.tick_size,
            )
            order_type = cfg["order_type"]
            price = 0.0
            if order_type == "SL":
                price = round_to_tick(
                    trigger * (1.0 + cfg["sl_limit_buffer_pct"] / 100.0),
                    leg.tick_size, Side.BUY,
                )
            request = OrderRequest(
                exchange=leg.exchange,
                tradingsymbol=leg.tradingsymbol,
                token=leg.token,
                side=Side.BUY,
                quantity=leg.quantity,
                order_type=order_type,
                price=price,
                trigger_price=trigger,
                tick_size=leg.tick_size,
                tag="optsell-psl",
            )
            order_id = self.broker.place(request)
            if order_id:
                leg.protective_sl_order_id = order_id
                placed += 1
                log.info("protective %s on %s trigger %.2f (entry %.2f)",
                         order_type, leg.tradingsymbol, trigger, leg.entry_price)
            else:
                log.warning("could not place protective stop on %s", leg.tradingsymbol)
        return placed

    def _cancel_protective(self, leg: Leg) -> None:
        if not leg.protective_sl_order_id:
            return
        if self.broker.cancel(leg.protective_sl_order_id):
            log.debug("cancelled protective stop %s", leg.protective_sl_order_id)
        leg.protective_sl_order_id = None

    def cancel_protective_stops(self, position: Position) -> None:
        for leg in position.legs:
            self._cancel_protective(leg)

    def protective_stop_triggered(self, position: Position) -> List[Leg]:
        """
        Legs whose broker-side disaster stop has already executed. If this ever
        returns anything, the bot's own stop did not get there first and the
        position must be reconciled immediately.
        """
        hit: List[Leg] = []
        for leg in position.short_legs:
            if not leg.protective_sl_order_id:
                continue
            snapshot = self.broker.status(leg.protective_sl_order_id)
            if snapshot.status is OrderStatus.COMPLETE and snapshot.filled_qty > 0:
                leg.exit_price = snapshot.avg_price
                leg.exit_time = clock.now()
                leg.state = LegState.CLOSED
                leg.protective_sl_order_id = None
                leg.note = "closed by protective stop"
                hit.append(leg)
                log.critical("PROTECTIVE STOP EXECUTED on %s @ %.2f",
                             leg.tradingsymbol, snapshot.avg_price)
        return hit

    # ══════════════════════════════════════════════════════════════════════════
    #  Marking
    # ══════════════════════════════════════════════════════════════════════════

    def refresh_marks(self, position: Position) -> None:
        """Conservative mark for every open leg (shorts at the ask, longs at the bid)."""
        for leg in position.open_legs:
            mark = self.ticks.mark_price(leg.token, leg.side)
            if mark > 0:
                leg.mark = mark
