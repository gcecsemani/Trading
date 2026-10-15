"""
engine.py
=========
NIFTY option-selling loop.

Every minute from 09:26 IST it stores open interest on ATM and seven strikes
either side. 09:15 through 09:25 is not read. The 09:26 print is the baseline.
The first change is 09:27. A strike already inside that band keeps its 09:26
open interest when spot later makes it at the money.

    ATM dominance since 09:26 must be past 30%
    The strike one step away on that side must be past 15%

Telegram gets that note, including support and resistance, every 5 minutes.
Nothing is traded before 09:41. From 09:41 a spread is sold when both checks
agree. Bullish sells the put. Bearish sells the call. On 0 DTE the sold
strike is ATM when its bid is above ₹100, otherwise the first of ITM1, ITM2
and ITM3 above ₹100. The cheap OTM hedge is bought first. The short is
stopped at +50 premium points. The hedge has no stop. The spread is closed
when the sold premium trades at or under the target, when both checks cross
the other way, or at the 15:15 square-off. That cross does not sell the new
side in the same minute. The next spread waits two minutes, then is taken
only if the signal still qualifies. Nothing new is opened after 15:00.
A restart rebuilds the minute tape from 09:26 before a new spread can be sold.
"""

from __future__ import annotations

import signal
import threading
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

import config
from analytics.series_store import fetch_window, load_day, oi_prints, save_day
from analytics.signals import (
    Snapshot,
    WindowRead,
    combined_read,
    confirming_ratio,
    flip_ready,
    opposite,
    trade_bias,
)
from core import clock
from core.enums import Bias, ExitReason, OptionType, Side
from core.logging_setup import get
from core.models import Position
from core.state_store import EngineStateStore
from data.chain import ChainBuilder
from data.instruments import InstrumentMaster
from data.kite_gateway import KiteGateway, TokenExpired
from data.tick_store import OrderUpdateStore, TickStore
from data.ws_feed import WebSocketFeed
from execution.live_broker import LiveBroker
from execution.order_router import OrderRouter
from execution.paper_broker import PaperBroker
from notify.telegram import TelegramNotifier
from risk.book import TradeBook
from strategies.spread import build_plan

log = get("engine")


class TradingEngine:
    def __init__(self) -> None:
        self.symbol = "NIFTY"
        self.symbols: List[str] = config.active_symbols()

        self.gateway = KiteGateway()
        self.instruments = InstrumentMaster(self.gateway)
        self.ticks = TickStore()
        self.order_updates = OrderUpdateStore()
        self.notifier = TelegramNotifier()
        self.feed = WebSocketFeed(self.ticks, self.order_updates, on_event=self._on_feed_event)
        self.chain = ChainBuilder(self.gateway, self.instruments, self.ticks, self.feed)

        if config.is_live():
            self.broker = LiveBroker(self.gateway, self.order_updates)
            log.critical("LIVE MODE — real orders will be placed")
        else:
            self.broker = PaperBroker(self.ticks, self.gateway)
            log.info("PAPER MODE — orders are simulated on the live book")

        self.router = OrderRouter(self.broker, self.ticks, on_order_failure=self._on_order_failure)
        self.book = TradeBook()
        self.state_store = EngineStateStore()

        self.positions: List[Position] = []
        self.closed: List[Position] = []
        self.samples: List[Snapshot] = []
        self._series_expiry = ""
        self._trade_expiry = None

        self._running = False
        self._stop_reason = "not started"
        self._last_state_save = 0.0
        self._eod_done_for: Optional[str] = None
        self._market_was_open = False
        self._shutdown_event = threading.Event()
        self._bucket: Dict[int, Optional[int]] = {
            int(config.ENGINE["scan_minutes"]): None,
            int(config.ENGINE["report_minutes"]): None,
        }
        self._bucket_hold: Dict[int, float] = {}
        self._feed_panic_alerted = False
        self._last_order_error = ""
        self._last_order_error_at = 0.0
        self._cooldown_until: Optional[datetime] = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> bool:
        problems = config.validate()
        for problem in problems:
            log.error("CONFIG: %s", problem)
        fatal = [p for p in problems if "paper mode" not in p and "Telegram" not in p]
        if fatal:
            log.critical("Refusing to start — %d configuration problem(s)", len(fatal))
            return False

        self.notifier.start()
        if not self.gateway.check_connection():
            self.notifier.error("Kite connection failed. Run python3 login_zerodha.py", critical=True)
            return False
        if not self.instruments.ensure_loaded():
            self.notifier.error("Could not load the NIFTY instrument master", critical=True)
            return False
        if not self.instruments.has_symbol(self.symbol):
            self.notifier.error("NIFTY options were not in the instrument dump", critical=True)
            return False
        expiry = self.instruments.select_expiry(self.symbol)
        if expiry is None:
            self.notifier.error("No NIFTY expiry inside the configured window", critical=True)
            return False

        log.info(
            "NIFTY expiry %s (%d DTE), lot %d",
            expiry, clock.dte(expiry), self.instruments.lot_size(self.symbol),
        )
        if not self.feed.start():
            self.notifier.error("Websocket did not connect", critical=True)
            return False

        self.chain.build(self.symbol)
        time.sleep(2.0)
        self._trade_expiry = expiry
        self._restore_state()
        self._restore_series(expiry)

        self.notifier.startup({
            "Expiry": f"{expiry} ({clock.dte(expiry)} DTE)",
            "Lot": self.instruments.lot_size(self.symbol),
            "Feed": f"{self.feed.subscribed_count} instruments",
        })
        self._running = True
        self._market_was_open = clock.is_market_open()
        return True

    def stop(self, reason: str = "manual") -> None:
        self._stop_reason = reason
        self._running = False
        self._shutdown_event.set()

    def shutdown(self) -> None:
        log.info("Shutting down (%s)", self._stop_reason)
        try:
            self._save_state()
        except Exception:
            log.exception("state save failed during shutdown")
        open_count = len(self._active())
        if open_count:
            self.notifier.error(
                f"{open_count} spread still open at shutdown. "
                "Restart the bot to resume it, or square it off in Kite.",
                critical=True,
            )
        self.feed.stop()
        self.notifier.shutdown(self._stop_reason, self.book.summary())
        self.notifier.stop()

    def install_signal_handlers(self) -> None:
        def handler(signum, frame):          # pragma: no cover
            log.warning("Signal %s received", signum)
            self.stop(f"signal {signum}")

        signal.signal(signal.SIGINT, handler)
        signal.signal(signal.SIGTERM, handler)

    def run(self) -> None:
        interval = config.ENGINE["monitor_interval_sec"]
        log.info(
            "Running — monitor %.1fs, scan every %d min, trade from %s",
            interval, config.ENGINE["scan_minutes"], config.SESSION["first_trade"],
        )
        while self._running:
            started = time.monotonic()
            try:
                self._cycle()
            except TokenExpired:
                self.notifier.error(
                    "Access token expired. Run python3 login_zerodha.py and restart. "
                    "Open spreads are not being managed until then.",
                    critical=True,
                )
                self.stop("token expired")
                break
            except Exception as exc:
                log.exception("Cycle error: %s", exc)
                self.notifier.error(f"Cycle error: {exc}")
                time.sleep(2.0)
            elapsed = time.monotonic() - started
            self._shutdown_event.wait(max(0.05, interval - elapsed))
        self.shutdown()

    # ── Clock ─────────────────────────────────────────────────────────────────

    def _cycle(self) -> None:
        self.book.roll_day_if_needed()
        if not clock.is_market_open():
            if self._market_was_open:
                self._end_of_day()
                self._market_was_open = False
            return
        if not self._market_was_open:
            log.info("Market open — analysis starts at %s", config.SESSION["analysis_start"])
            self._market_was_open = True
            self._eod_done_for = None

        self._monitor()
        if self._eod_done_for == clock.datestamp():
            self._save_state_if_due()
            return
        self._on_clock(clock.now())
        if clock.past_square_off() and not self._active():
            self._end_of_day()
            return
        self._save_state_if_due()

    def _save_state_if_due(self) -> None:
        now = time.monotonic()
        if now - self._last_state_save >= config.ENGINE["state_save_sec"]:
            self._last_state_save = now
            self._save_state()

    def _on_clock(self, now: datetime) -> None:
        elapsed = clock.elapsed_seconds(now)
        if elapsed is None:
            return
        # Collect first, then post. A 5-minute note on the same timestamp
        # then includes the print that just landed.
        self._fire_bucket(int(config.ENGINE["scan_minutes"]), elapsed, now, self._on_scan)
        self._fire_bucket(int(config.ENGINE["report_minutes"]), elapsed, now, self._on_report)

    def _fire_bucket(self, minutes: int, elapsed: int, now: datetime, handler) -> None:
        slot = elapsed // (minutes * 60)
        previous = self._bucket.get(minutes)
        opening = previous is None
        if opening and minutes != config.ENGINE["scan_minutes"]:
            # Longer blocks have not closed yet, so they wait for the first edge.
            self._bucket[minutes] = slot
            return
        if not opening and slot == previous:
            return
        if not opening and previous is not None and slot > previous + 1:
            log.warning(
                "Clock jumped: missed %d of the %d-minute updates",
                slot - previous - 1, minutes,
            )
        if time.monotonic() < self._bucket_hold.get(minutes, 0.0):
            return
        try:
            ok = handler(self._bucket_end(minutes, slot, now), now)
        except Exception:
            log.exception("%d-minute update failed", minutes)
            ok = False
        if ok is False:
            # A failed print stays on this slot and is tried again. The wait
            # keeps a dead quote from being called on every monitor pass.
            wait = float(config.ENGINE["scan_retry_sec"]) if minutes <= 1 else 20.0
            self._bucket_hold[minutes] = time.monotonic() + wait
            log.warning("%d-minute update did not land — retrying in %.0fs", minutes, wait)
            return
        self._bucket[minutes] = slot
        self._bucket_hold[minutes] = 0.0

    def _bucket_end(self, minutes: int, slot: int, now: datetime) -> datetime:
        return clock.analysis_start_dt(now) + timedelta(minutes=minutes * slot)

    # ── 1-minute print ────────────────────────────────────────────────────────

    def _on_scan(self, end: datetime, now: datetime) -> bool:
        snapshot = self._capture(end)
        if snapshot is None:
            log.warning("1-min scan at %s produced no usable OI", end.strftime("%H:%M"))
            return False
        self._remember(snapshot)
        read = combined_read(self.samples, end, clock.analysis_start_dt(end))
        log.info(
            "1m %s combined %s–%s spot %.2f ATM %d strikes %d | %s",
            end.strftime("%H:%M"), read.start.strftime("%H:%M"), end.strftime("%H:%M"),
            snapshot.spot, int(snapshot.atm), len(snapshot.ce_by_strike), read.bias,
        )
        for check in read.checks:
            log.info("  %s | %s", check.headline, check.detail.replace("\n", " | "))
        try:
            self._scan_decision(read, end)
        except Exception:
            log.exception("1-min trade decision failed")
            self.notifier.error(f"Trade decision failed at {end.strftime('%H:%M')}. The open spread is unchanged.")
        return True

    def _capture(self, stamp: datetime) -> Optional[Snapshot]:
        try:
            chain = self.chain.build(self.symbol)
        except Exception:
            log.exception("chain read failed at %s", stamp.strftime("%H:%M"))
            return None
        if chain is None:
            return None
        atm_key = int(round(float(chain.atm)))
        ce_by: dict = {}
        pe_by: dict = {}
        for strike in chain.strikes():
            key = int(round(float(strike)))
            ce = chain.get(strike, OptionType.CE)
            pe = chain.get(strike, OptionType.PE)
            if ce is not None and ce.oi > 0:
                ce_by[key] = int(ce.oi)
            if pe is not None and pe.oi > 0:
                pe_by[key] = int(pe.oi)

        if atm_key not in ce_by or atm_key not in pe_by:
            log.warning(
                "%s ATM OI print is thin (call %s, put %s)",
                stamp.strftime("%H:%M"),
                "present" if atm_key in ce_by else "missing",
                "present" if atm_key in pe_by else "missing",
            )
            return None
        return Snapshot(
            at=stamp,
            spot=chain.spot,
            atm=chain.atm,
            ce_oi=ce_by[atm_key],
            pe_oi=pe_by[atm_key],
            ce_by_strike=ce_by,
            pe_by_strike=pe_by,
        )

    def _scan_footer(self, end: datetime) -> str:
        first = clock.hhmm(config.SESSION["first_trade"])
        last = clock.hhmm(config.SESSION["last_entry"])
        if cooldown_active(end, self._cooldown_until):
            return (
                f"Flip cooldown until {self._cooldown_until.strftime('%H:%M')}. "
                "No new spread until then. The next one follows the signal at that minute."
            )
        if end.time() < first:
            return (
                f"Observing only, from {config.SESSION['analysis_start']}. "
                f"No trade before {config.SESSION['first_trade']}."
            )
        if end.time() > last:
            return (
                f"No new entry after {config.SESSION['last_entry']}. "
                f"Square-off is {config.SESSION['hard_square_off']}."
            )
        return (
            f"OI from {config.SESSION['analysis_start']}. "
            "ATM dominance past 30% and the strike one step away past 15% is the signal. "
            "Put writing sells the put. Call writing sells the call. "
            "A flip closes the spread, then waits "
            f"{int(config.ENGINE['flip_cooldown_min'])} min before the next decision."
        )

    def _scan_decision(self, read: WindowRead, end: datetime) -> None:
        """Open, or close on a qualified flip, once the 15-minute analysis has passed."""
        first = clock.hhmm(config.SESSION["first_trade"])
        if end.time() < first:
            return
        position = self._open_position()
        if position is not None:
            self._maybe_reverse(position, read, end)
            return

        stamp = end.strftime("%H:%M")
        if cooldown_active(end, self._cooldown_until):
            log.info(
                "%s flip cooldown until %s — no new spread",
                stamp, self._cooldown_until.strftime("%H:%M"),
            )
            return
        if self._cooldown_until is not None:
            self._cooldown_until = None
        need = int(config.ENGINE["min_open_samples"])
        if read.samples < need:
            log.info(
                "%s has %d prints from %s, need %d before a sell",
                stamp, read.samples, read.start.strftime("%H:%M"), need,
            )
            return
        side = trade_bias(read)
        if side is Bias.NEUTRAL:
            if read.bias in (Bias.BULLISH, Bias.BEARISH):
                log.info(
                    "%s ATM is %s but the nearby strike does not confirm — no new spread",
                    stamp, read.bias,
                )
            return
        if not self._baseline_ok():
            log.warning(
                "%s series starts at %s, not %s — no new spread until the open print is filled",
                stamp,
                self.samples[0].at.strftime("%H:%M") if self.samples else "—",
                config.SESSION["analysis_start"],
            )
            return

        if not self._can_open(end):
            if end.time() > clock.hhmm(config.SESSION["last_entry"]):
                text = (
                    f"{stamp} OI is {side}, "
                    f"but last entry was {config.SESSION['last_entry']}."
                )
            elif not self.ticks.is_healthy():
                text = f"{stamp} OI is {side}, but the feed is stale. No new spread."
            else:
                text = f"{stamp} OI is {side}, but this time is blacked out. No new spread."
            log.info(text)
            return
        text = f"{stamp} {_signal_text(read, side)}. Opening the spread."
        log.info(text)
        self.notifier.decision(text)
        self._try_open(read, side)

    def _maybe_reverse(self, position: Position, read: WindowRead, end: datetime) -> None:
        """Close when the other side qualifies. The next spread waits out the cooldown."""
        held = _position_bias(position)
        stamp = end.strftime("%H:%M")
        if position.entry_time is not None and position.entry_time >= end:
            log.info("%s just opened %s — the same minute cannot flip it", stamp, position.strategy)
            return
        if not self._baseline_ok():
            log.warning(
                "%s open print is missing — holding %s. Stop and target still apply.",
                stamp, position.strategy,
            )
            return
        if not flip_ready(read, held):
            if read.bias is opposite(held):
                log.info(
                    "%s ATM turned %s but the nearby strike does not confirm — holding %s",
                    stamp, read.bias, position.strategy,
                )
            else:
                log.info("%s dominance is %s, holding %s", stamp, read.bias, position.strategy)
            return
        wait = int(config.ENGINE["flip_cooldown_min"])
        why = (
            f"OI dominance turned to {read.bias} ({_pct(read.ratio)}) "
            f"and the nearby strike confirms ({_pct(confirming_ratio(read))})"
        )
        text = (
            f"{stamp} {why}. Closing {position.strategy}. "
            f"Next entry waits {wait} min, then only if the signal still qualifies."
        )
        log.info(text)
        self.notifier.decision(text)
        # Stored before the exit save. A restart in that gap must not skip the wait.
        self._cooldown_until = end + timedelta(minutes=wait)
        if not self._flatten(position, ExitReason.ADVERSE_FLIP, text):
            self._cooldown_until = None
            self._save_state()
            return
        self._save_state()

    def _baseline_ok(self) -> bool:
        """A new sell needs the 09:26 print. A restart that could not backfill does not guess."""
        return baseline_ready(self.samples)

    def _can_open(self, end: datetime) -> bool:
        last = clock.hhmm(config.SESSION["last_entry"])
        return (
            end.time() <= last
            and self.ticks.is_healthy()
            and not clock.in_blackout_window(end.time())
        )

    def _on_report(self, end: datetime, now: datetime) -> bool:
        """One Telegram note of the day-combined OI read. The 1-minute pass stays quiet."""
        minutes = int(config.ENGINE["report_minutes"])
        read = combined_read(self.samples, end, clock.analysis_start_dt(end))
        if read.samples <= 0:
            log.info("%d-min note skipped — no prints yet", minutes)
            return True
        log.info(
            "%dm note %s–%s %s",
            minutes, read.start.strftime("%H:%M"), end.strftime("%H:%M"), read.bias,
        )
        self.notifier.reading(
            f"{minutes} MIN OI",
            read,
            self._position_line(),
            self._scan_footer(end),
        )
        return True

    def _try_open(self, read: WindowRead, bias: Optional[Bias] = None) -> None:
        if self._open_position() is not None:
            return
        if cooldown_active(read.end, self._cooldown_until):
            log.info(
                "entry blocked — flip cooldown until %s",
                self._cooldown_until.strftime("%H:%M"),
            )
            return
        side = bias if bias in (Bias.BULLISH, Bias.BEARISH) else read.bias
        reasons = [check.headline for check in read.checks]
        reasons.insert(0, f"Day dominance is {side}")
        plan, note = build_plan(
            self.instruments, self.ticks, self.gateway,
            self.symbol, side, read.spot, reasons,
        )
        if plan is None:
            log.info("No entry — %s", note)
            self.notifier.decision(f"Qualified {side}, but the spread was not built. {note}")
            return

        log.warning("ENTERING %s %s", plan.strategy, plan.describe())
        position = self.router.enter(plan, read.spot)
        if position is None:
            self.notifier.error(f"{plan.strategy} entry failed and was rolled back", critical=True)
            return

        total = len(read.checks) or 2
        position.entry_view = {
            "bias": str(side),
            "votes": f"{read.agrees}/{total}",
            "span": read.span,
            "notes": " | ".join(reasons),
        }
        position.entry_reasons = reasons
        position.entry_score = float(read.agrees / total * 100)
        position.meta.update(plan.meta)
        self.feed.pin([leg.token for leg in position.legs])
        self.router.refresh_marks(position)
        self._track_pnl(position)
        self.positions.append(position)
        self.book.register_entry()
        self.notifier.entry(position)
        self._save_state()

    # ── Exits ─────────────────────────────────────────────────────────────────

    def _monitor(self) -> None:
        active = self._active()
        if not active:
            return
        feed_dead = (
            self.ticks.has_seen_tick()
            and self.ticks.last_tick_age() > config.ENGINE["feed_dead_panic_sec"]
        )
        if feed_dead and not self._feed_panic_alerted:
            age = self.ticks.last_tick_age()
            self.notifier.error(
                f"No ticks for {age:.0f}s. Flattening the open spread.",
                critical=True,
            )
            self._feed_panic_alerted = True
        if not feed_dead:
            self._feed_panic_alerted = False

        for position in list(self._active()):
            self.router.refresh_marks(position)
            self._track_pnl(position)
            position.exit_spot = self._spot() or position.exit_spot
            if _from_earlier_session(position):
                self._flatten(
                    position, ExitReason.TIME_SQUARE_OFF,
                    "left open from an earlier session",
                )
                continue
            if position.meta.get("wrong_expiry"):
                self._flatten(
                    position, ExitReason.TIME_SQUARE_OFF,
                    "saved spread is a different expiry from the one being read",
                )
                continue
            expiry = clock.is_expiry_day(position.expiry)
            if clock.past_square_off(expiry):
                self._flatten(position, ExitReason.TIME_SQUARE_OFF, "session square-off")
                continue
            if feed_dead:
                self._flatten(
                    position, ExitReason.FEED_DEAD,
                    f"no ticks for {self.ticks.last_tick_age():.0f}s",
                )
                continue
            reason = self._premium_exit(position)
            if reason is not None:
                self._flatten(position, reason[0], reason[1])

    def _premium_exit(self, position: Position) -> Optional[Tuple[ExitReason, str]]:
        shorts = position.short_legs
        if not shorts:
            return None
        short = shorts[0]
        tick = self.ticks.get(short.token)
        # A saved mark is not a live premium. Wait for a real print before
        # the stop or the target can fire, including right after a restart.
        if tick is None or tick.ltp <= 0:
            return None
        ltp = tick.ltp
        buy = tick.cover_price(Side.BUY, short.quantity) or tick.ask or 0.0
        touched = premium_triggers(ltp, buy)
        if not touched:
            return None
        sl = float(position.meta.get("sl_premium") or 0)
        target = float(position.meta.get("target_premium") or 0)
        # The stop uses the price we would have to pay to buy the short back,
        # when that price is close to the last trade. A wild ask does not count.
        if sl > 0 and max(touched) >= sl:
            return ExitReason.STOPLOSS, (
                f"{short.tradingsymbol} buy-back {max(touched):.2f} "
                f"hit the {sl:.2f} stop. Hedge is closed with the spread, not by its own stop."
            )
        # The target uses the premium print, or a buy-back that is already at it.
        if target > 0 and min(touched) <= target:
            return ExitReason.TARGET, (
                f"{short.tradingsymbol} premium {min(touched):.2f} "
                f"reached the {target:.2f} target."
            )
        return None

    def _flatten(self, position: Position, reason: ExitReason, detail: str) -> bool:
        self.router.cancel_protective_stops(position)
        ok = self.router.exit_position(position, reason)
        if not ok:
            log.error("%s exit did not finish — will retry", position.id)
            return False
        position.exit_spot = self._spot() or position.exit_spot
        self.feed.unpin([leg.token for leg in position.legs])
        if isinstance(self.broker, PaperBroker):
            self.broker.add_realised(position.total_pnl())
        self.book.register_exit(position)
        self.notifier.exit(position, reason, detail)
        self.positions = [p for p in self.positions if p.id != position.id]
        self.closed.append(position)
        self._save_state()
        return True

    def _track_pnl(self, position: Position) -> None:
        pnl = position.total_pnl()
        if not position.meta.get("marked"):
            position.peak_pnl = pnl
            position.trough_pnl = pnl
            position.meta["marked"] = True
            return
        position.peak_pnl = max(position.peak_pnl, pnl)
        position.trough_pnl = min(position.trough_pnl, pnl)

    def _position_line(self) -> str:
        position = self._open_position()
        if position is None:
            return "Position: flat"
        self.router.refresh_marks(position)
        self._track_pnl(position)
        short = position.short_legs[0] if position.short_legs else None
        hedge = position.long_legs[0] if position.long_legs else None
        parts = [f"Open {position.strategy}  P&L ₹{position.total_pnl():+,.0f}"]
        if short is not None:
            parts.append(
                f"Short {short.tradingsymbol} sold {short.entry_price:.2f} "
                f"now {short.mark:.2f}  stop {position.meta.get('sl_premium')}  "
                f"target ≤ {position.meta.get('target_premium')}"
            )
        if hedge is not None:
            parts.append(
                f"Hedge {hedge.tradingsymbol} bought {hedge.entry_price:.2f} "
                f"now {hedge.mark:.2f}  (no stop)"
            )
        return "\n".join(parts)

    # ── State ─────────────────────────────────────────────────────────────────

    def _open_position(self) -> Optional[Position]:
        return next((p for p in self.positions if p.is_active), None)

    def _active(self) -> List[Position]:
        return [p for p in self.positions if p.is_active]

    def _spot(self) -> float:
        token = self.instruments.spot_token(self.symbol)
        return self.ticks.ltp(token) if token else 0.0

    def _save_state(self) -> None:
        try:
            extra = {"closed_today": len(self.closed)}
            if self._cooldown_until is not None:
                extra["cooldown_until"] = self._cooldown_until.isoformat()
            self.state_store.save(
                [p.to_dict() for p in self._active()],
                extra=extra,
            )
        except Exception:
            log.exception("could not save state")

    def _remember(self, snapshot: Snapshot) -> None:
        minute = snapshot.at.strftime("%H:%M")
        self.samples = [sample for sample in self.samples if sample.at.strftime("%H:%M") != minute]
        self.samples.append(snapshot)
        self.samples.sort(key=lambda sample: sample.at)
        try:
            save_day(self.samples, expiry=self._series_expiry)
        except OSError:
            log.exception("Could not save the day's prints")

    def _restore_series(self, expiry) -> None:
        """Load today's prints, then fill any gap back to 09:26 from exchange OI."""
        self._series_expiry = expiry.isoformat()
        self.samples = load_day(expiry=self._series_expiry)
        self._drop_pre_open()
        saved = len(self.samples)
        added = 0
        try:
            added = self._backfill_oi(expiry)
        except TokenExpired:
            raise
        except Exception:
            log.exception("Open-interest rebuild failed")
        if not self._baseline_ok() and clock.now() > clock.analysis_start_dt():
            log.warning(
                "The %s print is still missing. Retrying the candle rebuild once.",
                config.SESSION["analysis_start"],
            )
            try:
                added += self._backfill_oi(expiry)
            except TokenExpired:
                raise
            except Exception:
                log.exception("Second open-interest rebuild failed")
        self._drop_pre_open()
        if added:
            self._remember_all()
        gaps = _sample_gaps(self.samples)
        if gaps:
            log.warning(
                "Open-interest minutes are missing (%s). "
                "Day dominance still uses the prints that are present.",
                ", ".join(gaps[:6]),
            )
        if not self.samples:
            log.info("No prints yet. The first one is at %s.", config.SESSION["analysis_start"])
            return
        read = combined_read(
            self.samples, self.samples[-1].at, clock.analysis_start_dt(self.samples[-1].at)
        )
        log.info(
            "Recovered %d saved prints and %d candles. Series %s–%s. Dominance %s (%s).",
            saved,
            added,
            self.samples[0].at.strftime("%H:%M"),
            self.samples[-1].at.strftime("%H:%M"),
            read.bias,
            _pct(read.ratio),
        )
        if not self._baseline_ok():
            log.warning(
                "Entries stay off until the %s print is in the series. "
                "An open spread is still stopped and targeted.",
                config.SESSION["analysis_start"],
            )

    def _drop_pre_open(self) -> None:
        """09:15 through 09:25 is not part of the series, including after an older file is loaded."""
        start = clock.analysis_start_dt()
        kept = [sample for sample in self.samples if sample.at >= start]
        dropped = len(self.samples) - len(kept)
        self.samples = kept
        if dropped:
            log.info(
                "Dropped %d prints from before %s",
                dropped, config.SESSION["analysis_start"],
            )

    def _remember_all(self) -> None:
        try:
            save_day(self.samples, expiry=self._series_expiry)
        except OSError:
            log.exception("Could not save the day's prints")

    def _backfill_oi(self, expiry) -> int:
        start = clock.analysis_start_dt()
        now = clock.now()
        if now <= start:
            return 0
        token = self.instruments.spot_token(self.symbol)
        if not token:
            return 0
        frm, to = fetch_window(start, now)
        log.info("Filling open interest from %s with exchange candles", start.strftime("%H:%M"))
        spot_rows = self.gateway.historical(token, frm, to, "minute")
        if not spot_rows:
            log.warning("No NIFTY minute candles — the series stays at the prints already saved")
            return 0

        each = int(config.symbol_cfg(self.symbol)["chain_strikes"])
        step = float(config.symbol_cfg(self.symbol)["strike_step"])
        spots = []
        for row in spot_rows:
            close = float(row.get("close") or 0)
            if close > 0:
                spots.append(close)
        if not spots:
            return 0
        lo = self.instruments.atm_strike(self.symbol, expiry, min(spots)) - each * step
        hi = self.instruments.atm_strike(self.symbol, expiry, max(spots)) + each * step
        oi_by = {}
        missed = 0
        for strike in self.instruments.strikes(self.symbol, expiry):
            if strike < lo - 1 or strike > hi + 1:
                continue
            for opt in (OptionType.CE, OptionType.PE):
                inst = self.instruments.option(self.symbol, expiry, strike, opt)
                if inst is None:
                    missed += 1
                    continue
                rows = self.gateway.historical(inst.token, frm, to, "minute", oi=True)
                if not rows:
                    missed += 1
                    continue
                oi_table = {}
                for row in rows:
                    at = row.get("date")
                    text = at.strftime("%H:%M") if isinstance(at, datetime) else str(at)[11:16]
                    if len(text) != 5:
                        continue
                    oi = int(row.get("oi") or 0)
                    if oi > 0:
                        oi_table[text] = oi
                if oi_table:
                    oi_by[(int(strike), str(opt))] = oi_table
                else:
                    missed += 1
        if missed:
            log.warning(
                "Open-interest history missing for %d contracts — those strikes are left out",
                missed,
            )
        if not oi_by:
            log.warning("No open-interest candles — live prints will build the series from here")
            return 0
        prints, enriched = oi_prints(
            spot_rows,
            oi_by,
            self.samples,
            start,
            now,
            each,
            step,
            lambda price: self.instruments.atm_strike(self.symbol, expiry, price),
        )
        if prints:
            self.samples.extend(prints)
            self.samples.sort(key=lambda sample: sample.at)
            log.info(
                "Added %d open-interest prints from %s to %s",
                len(prints), prints[0].at.strftime("%H:%M"), prints[-1].at.strftime("%H:%M"),
            )
        if enriched:
            log.info("Filled strikes missing from %d saved prints", enriched)
        if not prints and not enriched:
            log.warning("Exchange candles did not add any open-interest prints")
            return 0
        return len(prints) + enriched

    def _restore_cooldown(self, saved: dict) -> None:
        """A restart inside the two-minute wait must not open the next spread early."""
        if saved.get("stale_day"):
            return
        raw = saved.get("cooldown_until")
        if not raw:
            return
        try:
            until = datetime.fromisoformat(str(raw))
        except ValueError:
            log.warning("Ignoring an unreadable flip cooldown: %s", raw)
            return
        if until.tzinfo is None:
            until = clock.IST.localize(until)
        else:
            until = until.astimezone(clock.IST)
        if until > clock.now():
            self._cooldown_until = until
            log.info("Flip cooldown still runs until %s", until.strftime("%H:%M"))

    def _restore_state(self) -> None:
        saved = self.state_store.load() or {}
        self._restore_cooldown(saved)
        rows = saved.get("positions") or []
        if not rows:
            return
        carried = bool(saved.get("stale_day"))
        log.warning("Resuming %d saved spread(s)", len(rows))
        for row in rows:
            try:
                position = Position.from_dict(row)
            except Exception:
                log.exception("could not restore %s", row.get("id"))
                continue
            if not position.is_active:
                continue
            if carried:
                position.meta["carried_overnight"] = True
            elif getattr(self, "_trade_expiry", None) not in (None, position.expiry):
                position.meta["wrong_expiry"] = True
                log.warning(
                    "%s is %s, but the session is trading %s. It will be closed.",
                    position.strategy, position.expiry, self._trade_expiry,
                )
            self.positions.append(position)
            self.feed.pin([leg.token for leg in position.legs])
            self.router.refresh_marks(position)
        if self.positions:
            extra = (
                " It was still open after the session. It will be closed now."
                if carried else
                " Check it against Kite before trusting the P&L."
            )
            self.notifier.error(
                f"Resumed {len(self.positions)} open spread(s) from saved state." + extra,
                critical=True,
            )

    def _end_of_day(self) -> None:
        today = clock.datestamp()
        if self._eod_done_for == today:
            return
        self._eod_done_for = today
        log.info("END OF DAY")
        for position in list(self._active()):
            self._flatten(position, ExitReason.TIME_SQUARE_OFF, "end of session")
        summary = self.book.summary()
        try:
            from core.ledger import PerformanceLedger
            PerformanceLedger().ensure_day(today)
        except Exception:
            log.exception("ledger update failed")
        self.notifier.daily_summary(summary, len(self.closed))
        self.chain.reset_session()
        self.samples.clear()
        self.closed.clear()
        self._cooldown_until = None
        self._bucket = {
            int(config.ENGINE["scan_minutes"]): None,
            int(config.ENGINE["report_minutes"]): None,
        }
        self._bucket_hold = {}
        if self._active():
            # Do not wipe the file. A failed square-off has to be there tomorrow.
            self._save_state()
            self.notifier.error(
                "Square-off did not finish. The open spread is saved and will be "
                "closed when the market is open again.",
                critical=True,
            )
            return
        self.state_store.clear()

    def _on_feed_event(self, kind: str, detail: str) -> None:
        log.info("feed %s (%s)", kind, detail)
        self.notifier.feed_event(kind, detail)

    def _on_order_failure(self, message: str) -> None:
        now = time.monotonic()
        if message == self._last_order_error and now - self._last_order_error_at < 60:
            log.error("Order failure (telegram already sent): %s", message)
            return
        self._last_order_error = message
        self._last_order_error_at = now
        self.notifier.error(f"Order failure: {message}", critical=True)


def baseline_ready(samples: Sequence[Snapshot]) -> bool:
    """
    True when the first print is the 09:26 open and both of its ATM legs are present.

    A later first print, or a 09:26 print with only one ATM leg, is not a baseline.
    Entries and signal exits stay off until that print is filled. Stop and target do not use it.
    """
    if not samples:
        return False
    first = samples[0]
    open_at = clock.analysis_start_dt(first.at)
    lag = (first.at - open_at).total_seconds()
    # 09:27 is already the next minute. Only the 09:26 print is the baseline.
    if not 0 <= lag < 60:
        return False
    key = int(round(float(first.atm or 0)))
    if key <= 0:
        return False
    if first.ce_by_strike or first.pe_by_strike:
        return key in first.ce_by_strike and key in first.pe_by_strike
    return first.ce_oi > 0 and first.pe_oi > 0


def cooldown_active(end: datetime, until: Optional[datetime]) -> bool:
    """True while `end` is still inside the wait that follows a signal exit."""
    return until is not None and end < until


def premium_triggers(ltp: float, buy: float) -> List[float]:
    """
    Prices allowed to fire the stop or the target.

    The last trade always counts. The buy-back price counts only when it sits
    close to that trade. An ask far from the print is a bad tick: it must not
    stop the spread out, and it must not book the target.
    """
    prices: List[float] = []
    if ltp > 0:
        prices.append(ltp)
    if buy > 0 and (ltp <= 0 or abs(buy - ltp) <= max(5.0, ltp * 0.15)):
        prices.append(buy)
    return prices


def _from_earlier_session(position: Position) -> bool:
    if position.meta.get("carried_overnight"):
        return True
    if position.entry_time is not None and position.entry_time.date() < clock.today():
        return True
    return False


def _pct(ratio: Optional[float]) -> str:
    if ratio is None:
        return "—"
    return f"{ratio:+.0%}"


def _signal_text(read: WindowRead, side: Bias) -> str:
    """The sentence that says why this side is the trade."""
    strike = int(read.upper_strike if side is Bias.BEARISH else read.lower_strike)
    return (
        f"OI dominance is {side} ({_pct(read.ratio)}), "
        f"nearby {strike} confirms ({_pct(confirming_ratio(read))})"
    )


def _sample_gaps(samples: List[Snapshot]) -> List[str]:
    """Minute holes left after a restart. The day reading uses the prints that remain."""
    gaps: List[str] = []
    for prev, nxt in zip(samples, samples[1:]):
        minutes = (nxt.at - prev.at).total_seconds() / 60.0
        if minutes > 1.5:
            gaps.append(f"{prev.at.strftime('%H:%M')}–{nxt.at.strftime('%H:%M')}")
    return gaps


def _position_bias(position: Position) -> Bias:
    if position.strategy == "BULL_PUT":
        return Bias.BULLISH
    if position.strategy == "BEAR_CALL":
        return Bias.BEARISH
    return Bias.NEUTRAL
