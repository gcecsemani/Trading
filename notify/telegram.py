"""
notify/telegram.py
==================
Telegram notifications, sent from a background worker thread.

Sending is queued and never blocks the trading loops. That is not a cosmetic
decision: a Telegram timeout while a stop-loss is triggering would otherwise
delay the exit order. Risk management always wins over reporting.

Rate limiting respects Telegram's ~20 messages/minute channel budget; if the
queue overflows the *oldest non-critical* message is dropped rather than
delaying a fill report.
"""

from __future__ import annotations

import json
import queue
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

import config
from core import clock
from core.enums import Bias, ExitReason
from core.logging_setup import get
from core.models import Position
from analytics.signals import WindowRead, entry_ready

log = get("telegram")

SEP = "━━━━━━━━━━━━━━━"
MAX_LEN = 4000
QUEUE_MAX = 200


def esc(text: Any) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def money(value: float) -> str:
    return f"{value:+,.0f}" if value else "0"


class TelegramNotifier:
    def __init__(self) -> None:
        self.token = config.TELEGRAM_BOT_TOKEN
        self.chat_id = config.TELEGRAM_CHAT_ID
        self.enabled = bool(config.NOTIFY["enabled"] and self.token and self.chat_id)
        self._queue: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=QUEUE_MAX)
        self._stop = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self._sent_times: List[float] = []
        self.sent = 0
        self.failed = 0

        if not self.enabled:
            log.warning("Telegram disabled (missing token/chat id or NOTIFY.enabled=False)")

    # ─── Lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> None:
        if not self.enabled or self._worker is not None:
            return
        self._worker = threading.Thread(target=self._run, name="telegram", daemon=True)
        self._worker.start()

    def stop(self, drain_timeout: float = 8.0) -> None:
        if self._worker is None:
            return
        deadline = time.monotonic() + drain_timeout
        while not self._queue.empty() and time.monotonic() < deadline:
            time.sleep(0.2)
        self._stop.set()
        self._worker.join(timeout=3.0)
        self._worker = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            self._throttle()
            if self._post(item["text"]):
                self.sent += 1
            else:
                self.failed += 1
            self._queue.task_done()

    def _throttle(self) -> None:
        limit = config.NOTIFY["max_msgs_per_min"]
        now = time.monotonic()
        self._sent_times = [t for t in self._sent_times if now - t < 60.0]
        if len(self._sent_times) >= limit:
            sleep_for = 60.0 - (now - self._sent_times[0]) + 0.1
            if sleep_for > 0:
                log.debug("telegram throttle: sleeping %.1fs", sleep_for)
                time.sleep(sleep_for)
        self._sent_times.append(time.monotonic())

    def _post(self, text: str) -> bool:
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = urllib.parse.urlencode({
            "chat_id": self.chat_id,
            "text": text[:MAX_LEN],
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }).encode("utf-8")

        for attempt in range(config.NOTIFY["retry_attempts"]):
            request = urllib.request.Request(url, data=payload, method="POST")
            request.add_header("Content-Type",
                               "application/x-www-form-urlencoded; charset=utf-8")
            try:
                with urllib.request.urlopen(
                    request, timeout=config.NOTIFY["timeout_sec"]
                ) as response:
                    body = json.loads(response.read().decode("utf-8"))
                if body.get("ok"):
                    return True
                log.error("Telegram API error: %s", body)
            except urllib.error.HTTPError as exc:
                detail = ""
                try:
                    detail = exc.read().decode("utf-8")[:300]
                except Exception:
                    pass
                log.error("Telegram HTTP %s: %s", exc.code, detail)
                if exc.code == 429:
                    time.sleep(3.0 * (attempt + 1))
                    continue
                if 400 <= exc.code < 500:
                    return False          # malformed request: retrying will not help
            except Exception as exc:
                log.error("Telegram send error: %s", exc)
            time.sleep(1.0 + attempt)
        return False

    # ─── Enqueue ──────────────────────────────────────────────────────────────

    def send(self, text: str, critical: bool = False) -> None:
        if not self.enabled:
            return
        item = {"text": text, "critical": critical}
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            # Make room by discarding the oldest non-critical message.
            try:
                dropped = self._queue.get_nowait()
                log.warning("telegram queue full — dropped a %s message",
                            "critical" if dropped.get("critical") else "normal")
                self._queue.put_nowait(item)
            except (queue.Empty, queue.Full):
                log.error("telegram queue full — message dropped")

    def _header(self, emoji: str, title: str) -> str:
        mode = "LIVE" if config.is_live() else "PAPER"
        return f"{emoji} <b>{esc(title)}</b>  <code>[{mode}]</code>"

    # ══════════════════════════════════════════════════════════════════════════
    #  Messages
    # ══════════════════════════════════════════════════════════════════════════

    def startup(self, extra: Dict[str, Any]) -> None:
        if not config.NOTIFY["send_startup"]:
            return
        lines = [
            self._header("🟢", "NIFTY OPTION SELLING"),
            SEP,
            f"⏰ <code>{clock.now().strftime('%d-%b-%Y %H:%M:%S')} IST</code>",
            "One trade at a time. Bullish sells the put. Bearish sells the call.",
            f"Hedge is bought first: nearest OTM option under "
            f"<code>₹{config.TRADE['hedge_max_premium']:.0f}</code>.",
            f"The side is open interest only, from <code>{config.SESSION['analysis_start']}</code>. "
            "09:15–09:25 is not read. "
            "ATM dominance above 30% and the strike one step away above 15% is the signal. "
            "Put writing, confirmed by the strike below, sells the put. "
            "Call writing, confirmed by the strike above, sells the call. "
            "When both checks cross the other way, the spread is closed. "
            f"The next one waits <code>{int(config.ENGINE['flip_cooldown_min'])} min</code>, "
            "then follows the signal at that minute.",
            f"OI is read every <code>{config.ENGINE['scan_minutes']} min</code> "
            f"on ATM and <code>{int(config.SYMBOLS['NIFTY']['chain_strikes'])}</code> strikes either side. "
            f"Telegram gets support, resistance, and the read every "
            f"<code>{config.ENGINE['report_minutes']} min</code>.",
            f"No trade before <code>{config.SESSION['first_trade']}</code> or after "
            f"<code>{config.SESSION['last_entry']}</code>. "
            "On expiry day the contract expiring today is traded. "
            "If its ATM bid is not above ₹100, the first of ITM1, ITM2 and ITM3 above ₹100 is sold.",
            SEP,
            f"Analysis from <code>{config.SESSION['analysis_start']}</code> every "
            f"<code>{config.ENGINE['scan_minutes']} min</code>",
            f"First trade <code>{config.SESSION['first_trade']}</code>  "
            f"Last entry <code>{config.SESSION['last_entry']}</code>",
            f"Stop <code>+{config.TRADE['sl_points']:.0f}</code> on the sold premium only. "
            f"Target when that premium is <code>≤ {config.TRADE['target_premium']:.0f}</code>.",
            f"Square-off <code>{config.SESSION['hard_square_off']}</code>, then the day summary. "
            f"Lots <code>{config.TRADE['lots']}</code>. No daily loss cap, no trade cap.",
        ]
        for key, value in extra.items():
            lines.append(f"• {esc(key)}: <code>{esc(value)}</code>")
        if not config.is_live():
            lines.append("\n<i>Paper only. Orders are filled on the live book and are not sent to the exchange.</i>")
        else:
            lines.append("\n⚠️ <b>LIVE — real orders will be placed.</b>")
        self.send("\n".join(lines))

    def reading(self, title: str, read: WindowRead, position_line: str, footer: str) -> None:
        sending_signal = title.startswith("3 MIN") and config.NOTIFY["send_signals"]
        sending_block = (not title.startswith("3 MIN")) and config.NOTIFY["send_consolidated"]
        if not (sending_signal or sending_block):
            return
        emoji = {"BULLISH": "🟢", "BEARISH": "🔴", "NEUTRAL": "⚪"}.get(str(read.bias), "⚪")
        lines = [
            self._header(emoji, title),
            SEP,
            f"<code>{read.start.strftime('%H:%M')}–{read.end.strftime('%H:%M')}</code>  "
            f"spot <code>{read.spot:,.2f}</code>  ATM <code>{int(read.atm) if read.atm else 0}</code>  "
            f"<code>{read.samples}</code> prints",
            SEP,
        ]
        for check in read.checks:
            mark = {"BULLISH": "🟢", "BEARISH": "🔴", "NEUTRAL": "⚪"}.get(str(check.bias), "⚪")
            lines.append(f"{mark} <b>{esc(check.headline)}</b>")
            for row in check.detail.split("\n"):
                if row.startswith("SUPPORT") or row.startswith("RESISTANCE"):
                    lines.append(f"<b>{esc(row)}</b>")
                else:
                    lines.append(f"<code>{esc(row)}</code>")
        if entry_ready(read):
            verdict = f"{read.bias} — ATM is past 30% and the nearby strike is past 15%"
        elif read.bias in (Bias.BULLISH, Bias.BEARISH):
            verdict = f"ATM is {read.bias}, but the nearby strike is not past 15%"
        else:
            verdict = f"OI {read.bias} — ATM dominance is not past 30%"
        lines += [SEP, f"<b>{esc(verdict)}</b>"]
        if position_line:
            lines += [SEP, esc(position_line)]
        if footer:
            lines.append(esc(footer))
        self.send("\n".join(lines))

    def decision(self, text: str) -> None:
        if not config.NOTIFY["send_consolidated"]:
            return
        self.send(
            f"{self._header('🕐', 'DECISION')}\n{SEP}\n{esc(text)}",
            critical=True,
        )

    def entry(self, position: Position) -> None:
        if not config.NOTIFY["send_entries"]:
            return
        meta = position.meta or {}
        lines = [
            self._header("✅", f"OPENED {position.strategy}"),
            SEP,
            f"⏰ <code>{clock.stamp()}</code>  spot <code>{position.entry_spot:,.2f}</code>",
            f"<code>{esc(position.describe())}</code>",
        ]
        for leg in position.legs:
            if leg.is_short:
                book = meta.get("short_depth") or "sold into the bids"
                lines.append(
                    f"SOLD <code>{esc(leg.tradingsymbol)}</code> "
                    f"<code>{leg.lots} lot @ {leg.entry_price:.2f}</code>"
                )
                lines.append(f"<i>{esc(book)}</i>")
            else:
                book = meta.get("hedge_depth") or "bought from the asks"
                lines.append(
                    f"BOUGHT hedge <code>{esc(leg.tradingsymbol)}</code> "
                    f"<code>{leg.lots} lot @ {leg.entry_price:.2f}</code>"
                )
                lines.append(f"<i>{esc(book)}</i>")
        sl = meta.get("sl_premium")
        target = meta.get("target_premium", config.TRADE["target_premium"])
        lines += [
            SEP,
            f"Short stop <code>{sl}</code>  (+{config.TRADE['sl_points']:.0f} on the sold premium). "
            f"Hedge has no stop.",
            f"Target: buy the short back when its premium is <code>≤ {target}</code>.",
            f"Net credit <code>₹{position.credit:,.0f}</code>",
        ]
        if position.entry_reasons:
            lines.append(SEP)
            lines.extend(esc(reason) for reason in position.entry_reasons[:6])
        self.send("\n".join(lines), critical=True)

    def exit(self, position: Position, reason: ExitReason, detail: str) -> None:
        if not config.NOTIFY["send_exits"]:
            return
        pnl = position.total_pnl()
        emoji = "🟩" if pnl >= 0 else "🟥"
        lines = [
            self._header(emoji, f"CLOSED {position.strategy}"),
            SEP,
            f"⏰ <code>{clock.stamp()}</code>  held <code>{position.age_minutes():.0f} min</code>",
            f"Reason: <b>{esc(reason)}</b>",
        ]
        if detail:
            lines.append(esc(detail))
        lines.append(SEP)
        for leg in position.legs:
            verb = "Bought back" if leg.is_short else "Sold hedge"
            lines.append(
                f"{verb} <code>{esc(leg.tradingsymbol)}</code> "
                f"<code>{leg.exit_price:.2f}</code>  (entry {leg.entry_price:.2f})"
            )
        lines += [
            SEP,
            f"<b>P&amp;L ₹{pnl:+,.0f}</b>  costs <code>₹{position.costs:,.0f}</code>",
            f"Best <code>₹{position.peak_pnl:+,.0f}</code>  "
            f"worst <code>₹{position.trough_pnl:+,.0f}</code>",
        ]
        self.send("\n".join(lines), critical=True)

    def daily_summary(self, summary: Dict[str, Any], positions_closed: int) -> None:
        if not config.NOTIFY["send_daily_summary"]:
            return
        pnl = float(summary.get("realised_pnl") or 0.0)
        closed = summary.get("wins", 0) + summary.get("losses", 0)
        rate = (summary.get("wins", 0) / closed * 100.0) if closed else 0.0
        emoji = "🏆" if pnl > 0 else ("😐" if pnl == 0 else "🧊")
        lines = [
            self._header(emoji, "DAY COMPLETE"),
            SEP,
            f"📅 <code>{esc(summary.get('day'))}</code>",
            f"Net P&amp;L <b>₹{pnl:+,.0f}</b>  costs <code>₹{summary.get('costs', 0):,.0f}</code>",
            f"Closed trades <code>{positions_closed}</code>  "
            f"wins <code>{summary.get('wins', 0)}</code>  "
            f"losses <code>{summary.get('losses', 0)}</code>  "
            f"win rate <code>{rate:.0f}%</code>",
            "Everything open was squared off. No daily loss limit was applied.",
        ]
        self.send("\n".join(lines), critical=True)

    def error(self, message: str, critical: bool = False) -> None:
        if not config.NOTIFY["send_errors"]:
            return
        emoji = "🚨" if critical else "⚠️"
        self.send(
            f"{self._header(emoji, 'ENGINE ' + ('CRITICAL' if critical else 'WARNING'))}\n"
            f"{SEP}\n⏰ <code>{clock.stamp()}</code>\n<code>{esc(message)}</code>",
            critical=critical,
        )

    def feed_event(self, kind: str, detail: str) -> None:
        if kind in ("connected",):
            return
        emoji = {"closed": "🔌", "error": "⚠️", "reconnecting": "🔄", "dead": "🚨"}.get(kind, "ℹ️")
        self.send(
            f"{self._header(emoji, 'DATA FEED ' + kind.upper())}\n"
            f"{SEP}\n<code>{esc(detail)}</code>",
            critical=(kind == "dead"),
        )

    def shutdown(self, reason: str, summary: Dict[str, Any]) -> None:
        if not config.NOTIFY["send_shutdown"]:
            return
        self.send(
            f"{self._header('🔴', 'ENGINE STOPPED')}\n{SEP}\n"
            f"⏰ <code>{clock.now().strftime('%d-%b %H:%M:%S')}</code>\n"
            f"Reason: <code>{esc(reason)}</code>\n"
            f"Day P&amp;L <code>₹{summary.get('realised_pnl', 0):+,.0f}</code> "
            f"over <code>{summary.get('trades', 0)}</code> trade(s)",
            critical=True,
        )
