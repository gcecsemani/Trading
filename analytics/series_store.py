"""
analytics/series_store.py
=========================
The day's one-minute open-interest prints, on disk, so a restart continues
from the open print.

A missing stretch is rebuilt from exchange minute candles. A saved print that
never stored the strikes beside ATM is filled from those candles. The strikes
it already has are left alone. The live bid and ask used to price a hedge, a
stop, or a target stay on the websocket.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import config
from analytics.signals import Snapshot
from core import clock
from core.logging_setup import get

log = get("series")


def _path(day: Optional[str] = None) -> str:
    return os.path.join(config.STATE_DIR, f"samples_{(day or clock.datestamp())}.json")


def _dump(sample: Snapshot) -> dict:
    return {
        "at": sample.at.isoformat(),
        "spot": sample.spot,
        "atm": sample.atm,
        "ce_oi": sample.ce_oi,
        "pe_oi": sample.pe_oi,
        "ce_by_strike": {str(k): int(v) for k, v in sample.ce_by_strike.items()},
        "pe_by_strike": {str(k): int(v) for k, v in sample.pe_by_strike.items()},
    }


def _book(raw) -> Dict[int, int]:
    """Strike -> open interest. A bad key is skipped so one row cannot drop the day."""
    out: Dict[int, int] = {}
    for key, value in (raw or {}).items():
        try:
            strike = int(round(float(key)))
            oi = int(value)
        except (TypeError, ValueError):
            continue
        if oi > 0:
            out[strike] = oi
    return out


def _keep_saved_atm(sample: Snapshot) -> None:
    """
    An older print stored the ATM total beside an empty strike book.

    Put that total on the ATM strike before a candle fill runs, so the fill
    can add the wings and cannot hide the open interest that was already saved.
    """
    key = int(round(float(sample.atm or 0)))
    if key <= 0:
        return
    if key not in sample.ce_by_strike and int(sample.ce_oi or 0) > 0:
        sample.ce_by_strike[key] = int(sample.ce_oi)
    if key not in sample.pe_by_strike and int(sample.pe_oi or 0) > 0:
        sample.pe_by_strike[key] = int(sample.pe_oi)


def _load_one(row: dict) -> Optional[Snapshot]:
    try:
        at = as_ist(row.get("at"))
        if at is None:
            return None
        sample = Snapshot(
            at=at,
            spot=float(row.get("spot") or 0),
            atm=float(row.get("atm") or 0),
            ce_oi=int(row.get("ce_oi") or 0),
            pe_oi=int(row.get("pe_oi") or 0),
            ce_by_strike=_book(row.get("ce_by_strike")),
            pe_by_strike=_book(row.get("pe_by_strike")),
        )
    except (TypeError, ValueError):
        log.warning("Skipping a saved print that could not be read")
        return None
    _keep_saved_atm(sample)
    return sample


def as_ist(value) -> Optional[datetime]:
    """Candle timestamps from Kite are naive IST. Keep everything aware."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).replace("T", " ")[:19]
        try:
            dt = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    dt = dt.replace(second=0, microsecond=0)
    if dt.tzinfo is None:
        return clock.IST.localize(dt)
    return dt.astimezone(clock.IST).replace(second=0, microsecond=0)


def load_day(day: Optional[str] = None, expiry: Optional[str] = None) -> List[Snapshot]:
    path = _path(day)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError:
        return []
    except (json.JSONDecodeError, OSError) as exc:
        log.error("Could not read %s (%s)", path, exc)
        return []
    saved_expiry = None
    rows = raw
    if isinstance(raw, dict):
        saved_expiry = raw.get("expiry")
        rows = raw.get("prints") or []
    if expiry and saved_expiry and saved_expiry != expiry:
        log.info(
            "Saved prints are for %s, so they are not used for %s",
            saved_expiry, expiry,
        )
        return []
    if expiry and not saved_expiry and rows:
        log.info("Saved prints have no expiry tag; using them for %s", expiry)
    samples = []
    for row in rows if isinstance(rows, list) else []:
        sample = _load_one(row) if isinstance(row, dict) else None
        if sample is not None:
            samples.append(sample)
    samples.sort(key=lambda sample: sample.at)
    return samples


def save_day(
    samples: Sequence[Snapshot],
    day: Optional[str] = None,
    expiry: Optional[str] = None,
) -> None:
    """Merge with the file already on disk. A later print at the same minute wins."""
    merged = merge_samples(load_day(day, expiry=expiry), samples)
    path = _path(day)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    payload: object = [_dump(sample) for sample in merged]
    if expiry:
        payload = {"expiry": expiry, "prints": payload}
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
    os.replace(tmp, path)


def merge_samples(older: Sequence[Snapshot], newer: Sequence[Snapshot]) -> List[Snapshot]:
    by_minute: Dict[str, Snapshot] = {}
    for sample in list(older) + list(newer):
        by_minute[sample.at.strftime("%H:%M")] = sample
    return [by_minute[key] for key in sorted(by_minute)]


def _fill_missing(sample: Snapshot, ce_by: Dict[int, int], pe_by: Dict[int, int]) -> int:
    """
    Add strikes a saved print does not already have.

    A live quote is not replaced by a candle. Returns how many strikes were added.
    """
    added = 0
    for strike, oi in ce_by.items():
        if strike not in sample.ce_by_strike:
            sample.ce_by_strike[int(strike)] = int(oi)
            added += 1
    for strike, oi in pe_by.items():
        if strike not in sample.pe_by_strike:
            sample.pe_by_strike[int(strike)] = int(oi)
            added += 1
    return added


def _match(existing: Sequence[Snapshot], stamp: datetime, seconds: int = 45) -> Optional[Snapshot]:
    for sample in existing:
        if abs((sample.at - stamp).total_seconds()) < seconds:
            return sample
    return None


def _band(
    oi_by_strike: Dict[Tuple[int, str], Dict[str, int]],
    minute_key: str,
    atm: float,
    half: float,
) -> Tuple[Dict[int, int], Dict[int, int]]:
    ce_by: Dict[int, int] = {}
    pe_by: Dict[int, int] = {}
    for (strike, opt), table in oi_by_strike.items():
        if abs(strike - atm) > half + 1:
            continue
        oi = int(table.get(minute_key) or 0)
        if oi <= 0:
            continue
        if opt == "CE":
            ce_by[int(strike)] = oi
        elif opt == "PE":
            pe_by[int(strike)] = oi
    return ce_by, pe_by


def oi_prints(
    spot_candles: Sequence[dict],
    oi_by_strike: Dict[Tuple[int, str], Dict[str, int]],
    existing: Sequence[Snapshot],
    analysis_start: datetime,
    now: datetime,
    each_side: int,
    strike_step: float,
    atm_for: Callable[[float], float],
) -> Tuple[List[Snapshot], int]:
    """
    One print per exchange minute from the open through now.

    A minute already covered by a saved print is not replaced. Strikes that
    print never stored are filled from the candle, so a later ATM still has
    its open-interest trace. Returns (new prints, minutes that were filled in).
    """
    spot_at: Dict[str, Tuple[datetime, float]] = {}
    for row in spot_candles:
        at = as_ist(row.get("date"))
        close = float(row.get("close") or 0)
        if at is None or close <= 0 or at > now:
            continue
        spot_at[at.strftime("%H:%M")] = (at, close)
    if not spot_at:
        return [], 0

    ordered = [spot_at[key] for key in sorted(spot_at)]
    chosen: List[Tuple[datetime, datetime, float]] = []
    baseline = None
    for at, close in ordered:
        if at < analysis_start:
            baseline = (at, close)
            continue
        chosen.append((at, at, close))
    if baseline is not None and _match(existing, analysis_start) is None and not any(
        stamp == analysis_start for _src, stamp, _close in chosen
    ):
        chosen.insert(0, (baseline[0], analysis_start, baseline[1]))

    prints: List[Snapshot] = []
    enriched = 0
    half = each_side * strike_step
    for source, stamp, close in chosen:
        if stamp < analysis_start or stamp > now:
            continue
        if _match(prints, stamp) is not None:
            continue
        atm = float(atm_for(close))
        ce_by, pe_by = _band(oi_by_strike, source.strftime("%H:%M"), atm, half)
        if not ce_by and not pe_by:
            continue
        saved = _match(existing, stamp)
        if saved is not None:
            _keep_saved_atm(saved)
            if _fill_missing(saved, ce_by, pe_by):
                enriched += 1
            continue
        atm_key = int(round(atm))
        # The live minute is rejected unless both ATM legs printed. A candle
        # with only one of them would otherwise look like 100% dominance.
        if atm_key not in ce_by or atm_key not in pe_by:
            continue
        prints.append(Snapshot(
            at=stamp,
            spot=close,
            atm=atm,
            ce_oi=int(ce_by.get(atm_key, 0)),
            pe_oi=int(pe_by.get(atm_key, 0)),
            ce_by_strike=ce_by,
            pe_by_strike=pe_by,
        ))
    return prints, enriched


def fetch_window(from_dt: datetime, to_dt: datetime) -> Tuple[datetime, datetime]:
    """Include the minute before 09:26. Kite sometimes stamps that bar one step earlier."""
    return from_dt - timedelta(minutes=1), to_dt
