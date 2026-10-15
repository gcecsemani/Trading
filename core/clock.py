"""
core/clock.py
=============
Every time decision in the engine goes through here so the whole system shares
one notion of "now" in IST, independent of the machine's local timezone.
"""

from __future__ import annotations

from datetime import date, datetime, time as dtime
from typing import Optional

import pytz

import config

IST = pytz.timezone("Asia/Kolkata")


def now() -> datetime:
    """Timezone-aware current time in IST."""
    return datetime.now(IST)


def today() -> date:
    return now().date()


def now_time() -> dtime:
    return now().time()


def hhmm(value: str) -> dtime:
    """Parse "HH:MM" (or "HH:MM:SS") into a naive time object."""
    parts = [int(p) for p in value.strip().split(":")]
    while len(parts) < 3:
        parts.append(0)
    return dtime(parts[0], parts[1], parts[2])


def stamp(fmt: str = "%H:%M:%S") -> str:
    return now().strftime(fmt)


def datestamp() -> str:
    return now().strftime("%Y-%m-%d")


# ─── Session windows ──────────────────────────────────────────────────────────

def is_weekday(d: Optional[date] = None) -> bool:
    return (d or today()).weekday() < 5


def is_blackout_date(d: Optional[date] = None) -> bool:
    return (d or today()).isoformat() in set(config.SESSION.get("blackout_dates", []))


def in_blackout_window(t: Optional[dtime] = None) -> bool:
    t = t or now_time()
    for window in config.SESSION.get("blackout_windows", []):
        try:
            start, end = hhmm(window[0]), hhmm(window[1])
        except (TypeError, ValueError, IndexError):
            continue
        if start <= t <= end:
            return True
    return False


def is_market_open(t: Optional[datetime] = None) -> bool:
    t = t or now()
    if not is_weekday(t.date()):
        return False
    return hhmm(config.SESSION["market_open"]) <= t.time() <= hhmm(config.SESSION["market_close"])


def square_off_time(expiry_day: bool = False) -> dtime:
    key = "expiry_hard_square_off" if expiry_day else "hard_square_off"
    return hhmm(config.SESSION[key])


def past_square_off(expiry_day: bool = False, t: Optional[dtime] = None) -> bool:
    return (t or now_time()) >= square_off_time(expiry_day)


def session_open_dt(t: Optional[datetime] = None) -> datetime:
    t = t or now()
    o = hhmm(config.SESSION["market_open"])
    return t.replace(hour=o.hour, minute=o.minute, second=0, microsecond=0)


def analysis_start_dt(t: Optional[datetime] = None) -> datetime:
    """09:26 by default. That print is the open-interest baseline."""
    t = t or now()
    o = hhmm(config.SESSION["analysis_start"])
    return t.replace(hour=o.hour, minute=o.minute, second=0, microsecond=0)


def elapsed_seconds(t: Optional[datetime] = None) -> Optional[int]:
    """
    Whole seconds since analysis_start, or None before that and after the close.

    Scans line up on that clock (09:26, 09:27, 09:28, …) instead of whenever
    the process happened to start. The 09:26 print is kept. 09:15 through
    09:25 is not. Nothing is sold before the first-trade time.
    """
    t = t or now()
    if not is_weekday(t.date()) or is_blackout_date(t.date()):
        return None
    if t.time() < hhmm(config.SESSION["analysis_start"]):
        return None
    if t.time() > hhmm(config.SESSION["market_close"]):
        return None
    return int((t - analysis_start_dt(t)).total_seconds())


# ─── Expiry maths ─────────────────────────────────────────────────────────────

def as_date(value) -> Optional[date]:
    """Coerce a Kite expiry field (date / datetime / 'YYYY-MM-DD') to a date."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def dte(expiry) -> int:
    """Calendar days to expiry (0 = expiry today)."""
    exp = as_date(expiry)
    if exp is None:
        return 0
    return (exp - today()).days


def is_expiry_day(expiry) -> bool:
    return dte(expiry) == 0
