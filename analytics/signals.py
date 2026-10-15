"""
analytics/signals.py
====================
The only read this bot trades on: open interest.

The first print is 09:26. Minutes from 09:15 through 09:25 are not part of
the series. The first change is 09:27 against the 09:26 print.

Each print stores ATM and seven strikes either side. Dominance itself stays
on the current ATM call against the current ATM put. A strike that was
already inside that band keeps the open interest it had at 09:26, so a later
move in spot does not throw away the morning.

More than +30% ATM put writing is bullish. More than -30% ATM call writing
is bearish. A ratio sitting on 30% does not vote. The strike one step from
ATM on that side must agree, at half the threshold (15%). Bullish needs the
strike below. Bearish needs the strike above. The same pair of checks closes
a spread. The latest minute is shown. It does not decide.

Nothing here talks to the broker.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

import config
from core.enums import Bias


@dataclass
class Check:
    name: str
    bias: Bias
    headline: str
    detail: str


@dataclass
class Snapshot:
    """One minute of open interest. Prices and the bid/ask book are not stored."""

    at: datetime
    spot: float
    atm: float
    ce_oi: int
    pe_oi: int
    ce_by_strike: Dict[int, int] = field(default_factory=dict)
    pe_by_strike: Dict[int, int] = field(default_factory=dict)


@dataclass
class WindowRead:
    span: str
    start: datetime
    end: datetime
    samples: int
    checks: List[Check]
    bias: Bias
    bull_votes: int
    bear_votes: int
    spot: float = 0.0
    atm: float = 0.0
    speed: float = 0.0
    minute_bias: Bias = Bias.NEUTRAL
    minute_ratio: Optional[float] = None
    ratio: Optional[float] = None
    ratio_jump: float = 0.0
    lower_strike: float = 0.0
    upper_strike: float = 0.0
    lower_ratio: Optional[float] = None
    upper_ratio: Optional[float] = None
    lower_bias: Bias = Bias.NEUTRAL
    upper_bias: Bias = Bias.NEUTRAL

    @property
    def agrees(self) -> int:
        return max(self.bull_votes, self.bear_votes)

    @property
    def qualifies(self) -> bool:
        return self.bias in (Bias.BULLISH, Bias.BEARISH)


def dominance_ratio(bullish_qty: float, bearish_qty: float) -> Optional[float]:
    """
    Share of a two-sided move, from -1 to +1.

    +1 means `bullish_qty` is the whole move. None when both sides are zero.
    """
    denom = abs(bullish_qty) + abs(bearish_qty)
    if denom <= 0:
        return None
    return (bullish_qty - bearish_qty) / denom


def _bias_from_ratio(ratio: Optional[float], need: float) -> Bias:
    """Strictly more than `need`. A ratio sitting on the line does not vote."""
    if ratio is None or abs(ratio) <= need:
        return Bias.NEUTRAL
    if ratio > need:
        return Bias.BULLISH
    return Bias.BEARISH


def _pct(ratio: Optional[float]) -> str:
    if ratio is None:
        return "—"
    return f"{ratio:+.0%}"


def _qty(value: int) -> str:
    sign = "-" if value < 0 else ""
    n = abs(int(value))
    if n >= 10_000_000:
        return f"{sign}{n / 10_000_000:.2f}Cr"
    if n >= 100_000:
        return f"{sign}{n / 100_000:.2f}L"
    return f"{sign}{n:,}"


def _signed(value: int) -> str:
    if value > 0:
        return "+" + _qty(value)
    return _qty(value)


def opposite(bias: Bias) -> Bias:
    if bias is Bias.BULLISH:
        return Bias.BEARISH
    if bias is Bias.BEARISH:
        return Bias.BULLISH
    return Bias.NEUTRAL


def classify_oi(
    ce_change: int,
    pe_change: int,
    span: str = "",
    *,
    minute_ce: int = 0,
    minute_pe: int = 0,
    minute_ratio: Optional[float] = None,
    walls: str = "",
) -> Check:
    """Put-writing share versus call-writing share since the open print."""
    ratio = dominance_ratio(pe_change, ce_change)
    need = float(config.OI["min_ratio"])
    bias = _bias_from_ratio(ratio, need)
    window = f" over {span}" if span else ""
    opened = config.SESSION["analysis_start"]
    if ratio is None:
        change = f"no OI change since {opened}"
    else:
        change = (
            f"puts {_signed(pe_change)} · calls {_signed(ce_change)} · "
            f"dominance {_pct(ratio)}{window}  (need more than {need:.0%})"
        )
    minute = (
        f"last minute puts {_signed(minute_pe)} · calls {_signed(minute_ce)} · "
        f"{_pct(minute_ratio)}"
    )
    detail = change + "\n" + minute
    if walls:
        detail += "\n" + walls
    return Check("OI", bias, f"OI dominance {_pct(ratio)}  {bias}", detail)


def _atm_leg(sample: Snapshot, attr: str) -> Optional[Tuple[int, int]]:
    """The ATM strike and its open interest. Other strikes are left out."""
    book = getattr(sample, attr)
    if not book:
        return None
    strike = int(round(float(sample.atm)))
    if strike not in book:
        return None
    return strike, int(book[strike])


def _series(samples: Sequence[Snapshot], baseline: Optional[Snapshot]) -> List[Snapshot]:
    series = list(samples)
    if baseline is not None and baseline not in series:
        series = [baseline, *series]
    return series


def _first_seen(samples: Sequence[Snapshot], attr: str) -> Dict[int, int]:
    """First open interest stored for each strike. Later prints do not replace it."""
    found: Dict[int, int] = {}
    for sample in samples:
        for strike, oi in getattr(sample, attr).items():
            key = int(strike)
            if key not in found:
                found[key] = int(oi)
    return found


def _change_at(book: Dict[int, int], first: Dict[int, int], strike: int) -> int:
    """Change since the first print that already held this strike. A new strike is zero."""
    if strike not in book:
        return 0
    current = int(book[strike])
    return current - int(first.get(strike, current))


def _oi_change(
    samples: Sequence[Snapshot],
    baseline: Optional[Snapshot] = None,
) -> Tuple[int, int]:
    """
    Change in the current ATM call and the current ATM put.

    The baseline is the first print that already contained that strike, which
    is 09:26 when the strike was inside the tracked band at the open. Strikes
    beside ATM are not added into this total.
    """
    series = _series(samples, baseline)
    last = series[-1]
    if not last.ce_by_strike and not last.pe_by_strike:
        first = series[0]
        if first is last:
            return 0, 0
        return last.ce_oi - first.ce_oi, last.pe_oi - first.pe_oi
    atm = int(round(float(last.atm)))
    ce = _change_at(last.ce_by_strike, _first_seen(series, "ce_by_strike"), atm)
    pe = _change_at(last.pe_by_strike, _first_seen(series, "pe_by_strike"), atm)
    return ce, pe


def _pair_change(prev: Snapshot, last: Snapshot) -> Tuple[int, int]:
    """Change in the current ATM pair. A strike missing from the earlier print is left out."""
    ce_now = _atm_leg(last, "ce_by_strike")
    pe_now = _atm_leg(last, "pe_by_strike")
    if ce_now is None and pe_now is None:
        if not last.ce_by_strike and not last.pe_by_strike:
            return last.ce_oi - prev.ce_oi, last.pe_oi - prev.pe_oi
        return 0, 0
    ce = pe = 0
    if ce_now is not None:
        strike, oi = ce_now
        if strike in prev.ce_by_strike:
            ce = oi - int(prev.ce_by_strike[strike])
    if pe_now is not None:
        strike, oi = pe_now
        if strike in prev.pe_by_strike:
            pe = oi - int(prev.pe_by_strike[strike])
    return ce, pe


def _minute_speed(samples: Sequence[Snapshot]) -> Tuple[float, int, int, Optional[float]]:
    """
    Latest minute versus the average minute.

    A gap of several minutes is divided by its length, so one missing print
    does not look like a spike. Returns (speed, last call change, last put
    change, last minute dominance).
    """
    if len(samples) < 2:
        return 0.0, 0, 0, None
    paces: List[float] = []
    last_ce = last_pe = 0
    for prev, nxt in zip(samples, samples[1:]):
        ce, pe = _pair_change(prev, nxt)
        minutes = max(1, int(round((nxt.at - prev.at).total_seconds() / 60.0)))
        paces.append((abs(ce) + abs(pe)) / minutes)
        last_ce, last_pe = ce, pe
    average = sum(paces) / len(paces)
    speed = (paces[-1] / average) if average > 0 else 0.0
    return speed, last_ce, last_pe, dominance_ratio(last_pe, last_ce)


def _strike_step() -> int:
    return int(config.symbol_cfg("NIFTY")["strike_step"])


def _pair_ratio(
    ce_book: Dict[int, int],
    pe_book: Dict[int, int],
    ce_first: Dict[int, int],
    pe_first: Dict[int, int],
    strike: int,
) -> Tuple[bool, int, int, Optional[float]]:
    """
    Call change, put change, and dominance at one strike.

    Both legs must be on the print. A one-sided quote does not vote.
    """
    if strike not in ce_book or strike not in pe_book:
        return False, 0, 0, None
    ce = _change_at(ce_book, ce_first, strike)
    pe = _change_at(pe_book, pe_first, strike)
    return True, ce, pe, dominance_ratio(pe, ce)


def neighbor_confirms(read: WindowRead, bias: Bias) -> bool:
    """
    The strike one step from ATM must share `bias` at the half threshold.

    Bullish confirms on the strike below. Bearish confirms on the strike above.
    """
    if bias is Bias.BULLISH:
        return read.lower_bias is Bias.BULLISH
    if bias is Bias.BEARISH:
        return read.upper_bias is Bias.BEARISH
    return False


def confirming_ratio(read: WindowRead) -> Optional[float]:
    """Dominance of the strike that has to agree with the ATM side."""
    if read.bias is Bias.BEARISH:
        return read.upper_ratio
    return read.lower_ratio


def trade_bias(read: WindowRead, held: Optional[Bias] = None) -> Bias:
    """The side to sell. ATM past 30% and the nearby strike past 15%."""
    if held in (Bias.BULLISH, Bias.BEARISH) and flip_ready(read, held):
        return read.bias
    if held is None and entry_ready(read):
        return read.bias
    return Bias.NEUTRAL


def entry_ready(read: WindowRead) -> bool:
    """ATM dominance is past 30% and the first strike on that side is past 15%."""
    return read.bias in (Bias.BULLISH, Bias.BEARISH) and neighbor_confirms(read, read.bias)


def flip_ready(read: WindowRead, held: Bias) -> bool:
    """Close when the other side would itself qualify to open."""
    other = opposite(held)
    return other is not Bias.NEUTRAL and read.bias is other and neighbor_confirms(read, other)


_LEVEL_COUNT = 3


def _select_levels(
    book: Dict[int, int],
    first: Dict[int, int],
    spot: float,
    *,
    at_or_below: bool,
) -> List[Tuple[int, int, int]]:
    """
    (change, current OI, strike), strongest fresh writing first.

    The heaviest absolute wall is kept when it is not already in the top rows,
    so a large level that is not adding today is still visible.
    """
    rows: List[Tuple[int, int, int]] = []
    for strike, oi in book.items():
        key = int(strike)
        if at_or_below and key > spot:
            continue
        if not at_or_below and key < spot:
            continue
        current = int(oi)
        change = current - int(first.get(key, current))
        rows.append((change, current, key))
    rows.sort(key=lambda row: (row[0], row[1]), reverse=True)
    chosen = rows[:_LEVEL_COUNT]
    if rows:
        heaviest = max(rows, key=lambda row: (row[1], row[0]))
        if heaviest not in chosen:
            chosen.append(heaviest)
    return chosen


def _neighbor_line(
    label: str,
    strike: int,
    present: bool,
    ce_change: int,
    pe_change: int,
    ratio: Optional[float],
    nbias: Bias,
    *,
    required: bool,
    bias: Bias,
    bar: float,
) -> str:
    if not present:
        role = "confirm" if required else "watch"
        return f"Nearby {label} {strike} {role} · no open interest on this print"
    flag = ""
    if required:
        flag = " · aligns" if nbias is bias else " · does not align"
    role = "confirm" if required else "watch"
    return (
        f"Nearby {label} {strike} {role} · puts {_signed(pe_change)} · "
        f"calls {_signed(ce_change)} · dominance {_pct(ratio)}{flag}  "
        f"(need more than {bar:.0%})"
    )


def _level_block(
    title: str,
    rows: Sequence[Tuple[int, int, int]],
    kind: str,
) -> List[str]:
    lines = [title]
    if not rows:
        lines.append("none in the tracked strikes")
        return lines
    for change, oi, strike in rows:
        lines.append(f"{strike} {kind} {_signed(change)} now {_qty(oi)}")
    return lines


def _reading_lines(
    series: Sequence[Snapshot],
    span: str,
    ce_change: int,
    pe_change: int,
    ratio: Optional[float],
    bias: Bias,
    minute_ce: int,
    minute_pe: int,
    minute_ratio: Optional[float],
    lower: int,
    upper: int,
    lower_present: bool,
    upper_present: bool,
    lower_ce: int,
    lower_pe: int,
    upper_ce: int,
    upper_pe: int,
    lower_ratio: Optional[float],
    upper_ratio: Optional[float],
    lower_bias: Bias,
    upper_bias: Bias,
) -> Tuple[str, str]:
    """Headline plus the block posted every five minutes."""
    need = float(config.OI["min_ratio"])
    bar = float(config.OI["neighbor_ratio"])
    opened = config.SESSION["analysis_start"]
    atm = int(round(float(series[-1].atm)))
    window = f" over {span}" if span else ""
    if ratio is None:
        atm_line = f"ATM {atm} no OI change since {opened}"
    else:
        atm_line = (
            f"ATM {atm} puts {_signed(pe_change)} · calls {_signed(ce_change)} · "
            f"dominance {_pct(ratio)}{window}  (need more than {need:.0%})"
        )
    headline = f"ATM dominance {_pct(ratio)}  {bias}"
    if bias is Bias.BULLISH:
        word = "aligns" if lower_bias is Bias.BULLISH else "does not align"
        headline += f" · nearby {lower} {_pct(lower_ratio)} {word}"
    elif bias is Bias.BEARISH:
        word = "aligns" if upper_bias is Bias.BEARISH else "does not align"
        headline += f" · nearby {upper} {_pct(upper_ratio)} {word}"
    last = series[-1]
    ce_first = _first_seen(series, "ce_by_strike")
    pe_first = _first_seen(series, "pe_by_strike")
    supports = _select_levels(last.pe_by_strike, pe_first, last.spot, at_or_below=True)
    resists = _select_levels(last.ce_by_strike, ce_first, last.spot, at_or_below=False)
    lines = [
        atm_line,
        _neighbor_line(
            "below", lower, lower_present, lower_ce, lower_pe, lower_ratio, lower_bias,
            required=bias is Bias.BULLISH, bias=bias, bar=bar,
        ),
        _neighbor_line(
            "above", upper, upper_present, upper_ce, upper_pe, upper_ratio, upper_bias,
            required=bias is Bias.BEARISH, bias=bias, bar=bar,
        ),
        (
            f"last minute puts {_signed(minute_pe)} · calls {_signed(minute_ce)} · "
            f"{_pct(minute_ratio)}"
        ),
        *_level_block(
            f"SUPPORT  put change since {opened}, at or below spot",
            supports,
            "put",
        ),
        *_level_block(
            f"RESISTANCE  call change since {opened}, at or above spot",
            resists,
            "call",
        ),
    ]
    return headline, "\n".join(lines)


def classify_span(
    samples: Sequence[Snapshot],
    span: str,
    start: datetime,
    end: datetime,
    baseline: Optional[Snapshot] = None,
) -> WindowRead:
    """ATM dominance, the strike one step away, and support and resistance."""
    if not samples:
        return WindowRead(span, start, end, 0, [], Bias.NEUTRAL, 0, 0)

    last = samples[-1]
    series = _series(samples, baseline)
    ce_change, pe_change = _oi_change(samples, baseline)
    ratio = dominance_ratio(pe_change, ce_change)
    speed, minute_ce, minute_pe, minute_ratio = _minute_speed(series)
    need = float(config.OI["min_ratio"])
    bar = float(config.OI["neighbor_ratio"])
    bias = _bias_from_ratio(ratio, need)
    minute_bias = _bias_from_ratio(minute_ratio, need)
    jump = 0.0
    if len(series) >= 2 and ratio is not None:
        prev_ce, prev_pe = _oi_change(series[:-1], baseline=None)
        prev_ratio = dominance_ratio(prev_pe, prev_ce)
        if prev_ratio is not None:
            jump = ratio - prev_ratio

    step = _strike_step()
    atm = int(round(float(last.atm)))
    lower = atm - step
    upper = atm + step
    ce_first = _first_seen(series, "ce_by_strike")
    pe_first = _first_seen(series, "pe_by_strike")
    lower_present, lower_ce, lower_pe, lower_ratio = _pair_ratio(
        last.ce_by_strike, last.pe_by_strike, ce_first, pe_first, lower,
    )
    upper_present, upper_ce, upper_pe, upper_ratio = _pair_ratio(
        last.ce_by_strike, last.pe_by_strike, ce_first, pe_first, upper,
    )
    lower_bias = _bias_from_ratio(lower_ratio, bar)
    upper_bias = _bias_from_ratio(upper_ratio, bar)
    headline, detail = _reading_lines(
        series, span, ce_change, pe_change, ratio, bias,
        minute_ce, minute_pe, minute_ratio,
        lower, upper, lower_present, upper_present,
        lower_ce, lower_pe, upper_ce, upper_pe,
        lower_ratio, upper_ratio, lower_bias, upper_bias,
    )
    check = Check("OI", bias, headline, detail)
    return WindowRead(
        span=span,
        start=start,
        end=end,
        samples=len(samples),
        checks=[check],
        bias=bias,
        bull_votes=1 if bias is Bias.BULLISH else 0,
        bear_votes=1 if bias is Bias.BEARISH else 0,
        spot=last.spot,
        atm=last.atm,
        speed=speed,
        minute_bias=minute_bias,
        minute_ratio=minute_ratio,
        ratio=ratio,
        ratio_jump=jump,
        lower_strike=float(lower),
        upper_strike=float(upper),
        lower_ratio=lower_ratio,
        upper_ratio=upper_ratio,
        lower_bias=lower_bias,
        upper_bias=upper_bias,
    )


def combined_read(
    samples: Sequence[Snapshot],
    end: datetime,
    session_open: datetime,
) -> WindowRead:
    """
    OI change from the open print through `end`.

    Prints before that print are dropped. This is the whole series, not a
    trailing slice.
    """
    inside = [sample for sample in samples if session_open <= sample.at <= end]
    if not inside:
        return WindowRead("1m", session_open, end, 0, [], Bias.NEUTRAL, 0, 0)
    start = inside[0].at
    minutes = max(1, int((end - start).total_seconds() // 60))
    return classify_span(inside, config.oi_span(minutes), start, end, baseline=None)
