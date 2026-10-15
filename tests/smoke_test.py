"""
Offline checks for the NIFTY selling rules.

No broker, no market hours. Run with:

    python3 tests/smoke_test.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytz

import config
from analytics.signals import (
    Snapshot,
    WindowRead,
    classify_oi,
    combined_read,
    entry_ready,
    flip_ready,
    trade_bias,
)
from core.enums import Bias, LegRole, LegState, OptionType, OrderStatus, PositionState, Side
from core.models import BookLevel, Leg, Position, Tick
from core.state_store import EngineStateStore
from data.tick_store import TickStore
from engine import premium_triggers
from execution.broker_base import OrderSnapshot
from execution.live_broker import prefer_snapshot
from strategies.spread import choose_zero_dte_short, pick_hedge

IST = pytz.timezone("Asia/Kolkata")
_TMP = tempfile.mkdtemp(prefix="nifty-sell-")
config.STATE_DIR = _TMP
config.PATHS["state_file"] = os.path.join(_TMP, "engine_state.json")
config.PATHS["day_stats_file"] = os.path.join(_TMP, "day_stats.json")
config.PATHS["trade_journal"] = os.path.join(_TMP, "trades.csv")
config.PATHS["ledger_daily"] = os.path.join(_TMP, "ledger_daily.csv")
config.PATHS["ledger_monthly"] = os.path.join(_TMP, "ledger_monthly.csv")
config.PATHS["log_dir"] = os.path.join(_TMP, "logs")
config.LOGGING["level"] = "WARNING"
config.NOTIFY["enabled"] = False

PASSED = 0
FAILED = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print(f"  ok  {name}")
    else:
        FAILED += 1
        print(f"FAIL  {name}  {detail}")


def at(hour: int, minute: int) -> datetime:
    return IST.localize(datetime(2026, 10, 12, hour, minute, 0))


def snap(hour, minute, ce_oi, pe_oi, atm=24500.0) -> Snapshot:
    """ATM plus the strike either side, so a normal test also meets the 15% check."""
    strike = int(round(float(atm)))
    step = int(config.symbol_cfg("NIFTY")["strike_step"])
    offsets = (-step, 0, step)
    return Snapshot(
        at=at(hour, minute),
        spot=float(atm),
        atm=float(atm),
        ce_oi=int(ce_oi),
        pe_oi=int(pe_oi),
        ce_by_strike={strike + offset: int(ce_oi) for offset in offsets},
        pe_by_strike={strike + offset: int(pe_oi) for offset in offsets},
    )


def walk(start_h, start_m, steps, ce0, pe0, ce_step, pe_step) -> list:
    """One print per minute. `steps` includes the baseline."""
    out = []
    ce, pe = ce0, pe0
    hour, minute = start_h, start_m
    for _ in range(steps):
        out.append(snap(hour, minute, ce, pe))
        minute += 1
        while minute >= 60:
            hour += 1
            minute -= 60
        ce += ce_step
        pe += pe_step
    return out


def test_oi():
    bull = classify_oi(ce_change=-10_000, pe_change=40_000, span="45m")
    bear = classify_oi(ce_change=50_000, pe_change=5_000, span="45m")
    noise = classify_oi(ce_change=10_000, pe_change=12_000, span="45m")
    exact = classify_oi(ce_change=35_000, pe_change=65_000, span="45m")
    over = classify_oi(ce_change=34_000, pe_change=66_000, span="45m")
    check("put writing over call writing is bullish", bull.bias is Bias.BULLISH, bull.headline)
    check("call writing over put writing is bearish", bear.bias is Bias.BEARISH, bear.headline)
    check("a small OI share stays neutral", noise.bias is Bias.NEUTRAL, noise.headline)
    check("OI dominance of exactly 30% does not vote", exact.bias is Bias.NEUTRAL, exact.headline)
    check("OI dominance above 30% is bullish", over.bias is Bias.BULLISH, over.headline)


def test_series_survives_restart():
    from analytics.series_store import load_day, oi_prints, save_day
    early = snap(9, 15, 1_000_000, 2_000_000)
    early.ce_by_strike = {22500: 1_000_000}
    early.pe_by_strike = {22500: 2_000_000}
    save_day([early], day="2026-10-13")
    loaded = load_day("2026-10-13")
    check(
        "a saved print is there after a restart",
        len(loaded) == 1 and loaded[0].pe_by_strike[22500] == 2_000_000,
    )
    tagged = load_day("2026-10-13", expiry="2026-10-15")
    check(
        "a print saved before expiry tags is still used",
        len(tagged) == 1 and tagged[0].pe_by_strike[22500] == 2_000_000,
    )

    candles = [
        {"date": at(9, 14), "close": 22460},
        {"date": at(9, 16), "close": 22470},
        {"date": at(9, 17), "close": 22480},
        {"date": at(9, 18), "close": 22490},
    ]
    oi = {
        (22450, "CE"): {"09:14": 1_000_000, "09:16": 1_100_000, "09:17": 1_200_000, "09:18": 1_300_000},
        (22450, "PE"): {"09:14": 2_000_000, "09:16": 2_100_000, "09:17": 2_200_000, "09:18": 2_300_000},
        (22500, "CE"): {"09:14": 500_000, "09:16": 600_000, "09:17": 700_000, "09:18": 800_000},
        (22500, "PE"): {"09:14": 500_000, "09:16": 700_000, "09:17": 900_000, "09:18": 1_100_000},
    }
    filled, _enriched = oi_prints(
        candles, oi, loaded, at(9, 15), at(9, 18),
        each_side=5, strike_step=50, atm_for=lambda price: round(price / 50) * 50,
    )
    check(
        "each missing minute is filled and the saved 09:15 print is left alone",
        [sample.at for sample in filled] == [at(9, 16), at(9, 17), at(9, 18)],
        str([sample.at.strftime("%H:%M") for sample in filled]),
    )
    check("the saved open interest is not replaced by a candle", loaded[0].ce_oi == 1_000_000)

    one_sided, _skipped = oi_prints(
        [{"date": at(9, 26), "close": 24510}],
        {
            (24500, "CE"): {"09:26": 1_000_000},
            (24450, "CE"): {"09:26": 500_000},
            (24450, "PE"): {"09:26": 500_000},
        },
        [],
        at(9, 26),
        at(9, 26),
        each_side=7,
        strike_step=50,
        atm_for=lambda price: 24500.0,
    )
    check("a minute missing the ATM put is not stored", one_sided == [])

    bare = snap(9, 26, 1_000_000, 2_000_000)
    bare.ce_by_strike = {}
    bare.pe_by_strike = {}
    save_day([bare], day="2026-10-11")
    restored = load_day("2026-10-11", expiry="2026-10-16")
    wings, enriched = oi_prints(
        [{"date": at(9, 26), "close": 24510}],
        {
            (24500, "CE"): {"09:26": 9_000_000},
            (24500, "PE"): {"09:26": 9_000_000},
            (24450, "CE"): {"09:26": 100},
            (24450, "PE"): {"09:26": 100},
        },
        restored,
        at(9, 26),
        at(9, 26),
        each_side=7,
        strike_step=50,
        atm_for=lambda price: 24500.0,
    )
    check(
        "a saved ATM total is kept and the missing wing is filled",
        wings == []
        and enriched == 1
        and restored[0].ce_by_strike.get(24500) == 1_000_000
        and restored[0].pe_by_strike.get(24500) == 2_000_000
        and restored[0].ce_by_strike.get(24450) == 100,
    )
    save_day(restored, day="2026-10-10", expiry="2026-10-16")
    other = load_day("2026-10-10", expiry="2026-10-23")
    check("prints for a different expiry are not reused", other == [])


def test_oi_stays_on_the_same_strikes():
    """ATM is the dominance total. A strike with no morning print does not invent one."""
    early = snap(9, 26, 0, 0)
    early.atm = 24500
    early.ce_by_strike = {24500: 1_000_000, 24400: 9_000_000}
    early.pe_by_strike = {24500: 1_000_000, 24400: 1_000_000}
    later = snap(9, 27, 0, 0)
    later.atm = 24500
    later.ce_by_strike = {24500: 1_300_000, 24450: 8_000_000}
    later.pe_by_strike = {24500: 2_800_000, 24450: 500_000}
    read = combined_read([early, later], later.at, at(9, 26))
    oi = read.checks[0]
    check(
        "OI change uses the ATM strike and does not add the strikes beside it",
        oi.bias is Bias.BULLISH and "puts +18.00L" in oi.detail and "calls +3.00L" in oi.detail,
        oi.detail,
    )
    moved = snap(9, 28, 0, 0)
    moved.atm = 24550
    moved.spot = 24550
    moved.ce_by_strike = {24550: 5_000_000}
    moved.pe_by_strike = {24550: 4_000_000}
    shifted = combined_read([early, later, moved], moved.at, at(9, 26))
    check(
        "a strike with no earlier print does not count the OI it already had",
        shifted.bias is Bias.NEUTRAL and "no OI change" in shifted.checks[0].detail,
        shifted.checks[0].detail,
    )


def test_morning_trace_follows_the_strike():
    """A strike stored at 09:26 keeps that baseline after it becomes ATM."""
    early = snap(9, 26, 0, 0)
    early.atm = 24500
    early.spot = 24510
    early.ce_by_strike = {24500: 1_000_000, 24550: 2_000_000, 24600: 500_000}
    early.pe_by_strike = {24500: 1_000_000, 24550: 1_000_000, 24600: 400_000}
    later = snap(10, 0, 0, 0)
    later.atm = 24550
    later.spot = 24560
    later.ce_by_strike = {24500: 1_100_000, 24550: 2_200_000, 24600: 900_000}
    later.pe_by_strike = {24500: 1_500_000, 24550: 1_800_000, 24600: 700_000}
    read = combined_read([early, later], later.at, at(9, 26))
    detail = read.checks[0].detail
    check(
        "the new ATM uses the open interest it had at 09:26",
        read.bias is Bias.BULLISH and "puts +8.00L" in detail and "calls +2.00L" in detail,
        detail,
    )
    check(
        "the strike above the new ATM is on the same morning baseline",
        "24600" in detail and "call +4.00L" in detail,
        detail,
    )


def test_walls():
    sample = snap(9, 26, 0, 0)
    sample.ce_by_strike = {24500: 1_000_000, 24600: 4_000_000, 24400: 500_000}
    sample.pe_by_strike = {24500: 800_000, 24400: 5_000_000, 24600: 200_000}
    later = snap(9, 27, 0, 0)
    later.ce_by_strike = {24500: 1_100_000, 24600: 4_200_000, 24400: 500_000}
    later.pe_by_strike = {24500: 900_000, 24400: 5_400_000, 24600: 200_000}
    read = combined_read([sample, later], later.at, at(9, 26))
    detail = read.checks[0].detail
    check("the OI check names the ATM strike", "ATM 24500" in detail, detail)
    check("the ATM call open interest is shown", "11.00L" in detail, detail)
    check("support is the put buildup at or below spot", "SUPPORT" in detail and "24400" in detail, detail)
    check("resistance is the call buildup at or above spot", "RESISTANCE" in detail and "24600" in detail, detail)


def test_dominance_alone():
    built = walk(9, 15, 16, 1_000_000, 1_000_000, 2_000, 20_000)
    read = combined_read(built, built[-1].at, at(9, 15))
    check("steady put writing is bullish", read.bias is Bias.BULLISH, read.checks[0].headline)
    check("dominance past 30% can open", entry_ready(read))
    check("the same dominance does not flip", not flip_ready(read, Bias.BULLISH))

    slow = walk(9, 15, 15, 1_000_000, 1_000_000, 2_000, 20_000)
    stall = snap(9, 30, slow[-1].ce_oi + 200, slow[-1].pe_oi + 2_000)
    faded = combined_read(slow + [stall], stall.at, at(9, 15))
    check("a slow minute still qualifies while the day is past 30%", entry_ready(faded), faded.checks[0].headline)

    spiked = list(built)
    spiked.append(snap(9, 31, built[-1].ce_oi + 200_000, built[-1].pe_oi + 1_000))
    spike = combined_read(spiked, spiked[-1].at, at(9, 15))
    check("one bearish minute does not close a trade the day has not given up",
          spike.minute_bias is Bias.BEARISH and not flip_ready(spike, Bias.BULLISH),
          f"day {spike.bias} minute {spike.minute_bias}")
    held = WindowRead(
        span="16m", start=at(9, 26), end=at(9, 42), samples=16, checks=[],
        bias=Bias.BULLISH, bull_votes=1, bear_votes=0,
        minute_bias=Bias.BEARISH, minute_ratio=-0.90,
    )
    check("dominance still bullish keeps the put spread", not flip_ready(held, Bias.BULLISH))
    turned_atm = WindowRead(
        span="40m", start=at(9, 26), end=at(10, 10), samples=40, checks=[],
        bias=Bias.BEARISH, bull_votes=0, bear_votes=1,
    )
    check(
        "ATM dominance the other way does not flip without the strike above",
        not flip_ready(turned_atm, Bias.BULLISH),
    )
    turned = WindowRead(
        span="40m", start=at(9, 26), end=at(10, 10), samples=40, checks=[],
        bias=Bias.BEARISH, bull_votes=0, bear_votes=1,
        upper_bias=Bias.BEARISH, upper_strike=24550,
    )
    check(
        "ATM and the strike above, both the other way, are a flip",
        flip_ready(turned, Bias.BULLISH) and trade_bias(turned, Bias.BULLISH) is Bias.BEARISH,
    )
    bare = WindowRead(
        span="40m", start=at(9, 26), end=at(10, 10), samples=40, checks=[],
        bias=Bias.BULLISH, bull_votes=1, bear_votes=0,
    )
    check("ATM dominance alone does not open", not entry_ready(bare) and trade_bias(bare) is Bias.NEUTRAL)
    day_only = WindowRead(
        span="40m", start=at(9, 26), end=at(10, 10), samples=40, checks=[],
        bias=Bias.BULLISH, bull_votes=1, bear_votes=0,
        lower_bias=Bias.BULLISH, lower_strike=24450,
    )
    check(
        "ATM and the strike below can open",
        entry_ready(day_only) and trade_bias(day_only) is Bias.BULLISH,
    )


def test_combined_open():
    """Prints before 09:26 are dropped. Put writing from the open can qualify."""
    before = snap(9, 25, 9_000_000, 1_000_000)
    samples = [before] + walk(9, 26, 15, 2_000_000, 2_000_000, 1_000, 12_000)
    check("the series reaches 09:40", samples[-1].at == at(9, 40), str(samples[-1].at))
    read = combined_read(samples, at(9, 40), at(9, 26))
    check("the read starts at 09:26", read.start == at(9, 26), str(read.start))
    check("the 09:25 print is ignored", read.samples == 15, str(read.samples))
    check("09:26 to 09:40 is labelled 14m", read.span == "14m", read.span)
    check("put writing past 30%, with the strike below, qualifies", entry_ready(read), read.checks[0].headline)


def test_combined_reversal():
    """Call writing can pull the day through 30% the other way, and the minute confirms."""
    samples = walk(9, 15, 6, 1_000_000, 1_000_000, 1_000, 20_000)
    early = combined_read(samples, samples[-1].at, at(9, 15))
    check("the opening minutes are bullish", early.bias is Bias.BULLISH, early.checks[0].headline)
    ce = samples[-1].ce_oi
    pe = samples[-1].pe_oi
    hour, minute = 9, 21
    for _ in range(12):
        ce += 40_000
        samples.append(snap(hour, minute, ce, pe))
        minute += 1
    later = combined_read(samples, samples[-1].at, at(9, 15))
    check("the day flips to call writing", later.bias is Bias.BEARISH, later.checks[0].headline)
    check("dominance the other way closes the spread", flip_ready(later, Bias.BULLISH), later.checks[0].headline)


def test_neighbor_on_the_line_does_not_open():
    """The strike below must be strictly past 15%. Sitting on 15% does not open."""
    first = snap(9, 26, 1_000_000, 1_000_000)
    second = snap(9, 40, 1_000_000, 1_000_000)
    second.ce_by_strike = {24500: 1_050_000, 24450: 1_085_000, 24550: 1_050_000}
    second.pe_by_strike = {24500: 1_200_000, 24450: 1_115_000, 24550: 1_200_000}
    read = combined_read([first, second], second.at, at(9, 26))
    check("ATM put writing is bullish", read.bias is Bias.BULLISH, read.checks[0].headline)
    check(
        "15% exactly on the strike below does not open",
        not entry_ready(read),
        read.checks[0].headline,
    )
    second.pe_by_strike[24450] = 1_116_000
    opened = combined_read([first, second], second.at, at(9, 26))
    check(
        "the strike below past 15% can open",
        entry_ready(opened) and opened.lower_bias is Bias.BULLISH,
        opened.checks[0].headline,
    )


def test_hedge_pick():
    # Nearest first. 80 and 40 are too dear. 9.5 is the first under 10. 4.0 is further.
    ladder = [(25000, 80.0), (25050, 40.0), (25100, 10.0), (25150, 9.5), (25200, 4.0)]
    chosen = pick_hedge(ladder, max_premium=10.0, min_premium=0.05)
    check("hedge is the nearest ask strictly under 10", chosen == 25150, str(chosen))
    check("an ask of exactly 10 is not used", pick_hedge([(25100, 10.0)], 10, 0.05) is None)


def test_depth_walk():
    tick = Tick(
        token=1,
        ltp=100,
        bid=99.5,
        ask=100.5,
        bid_qty=30,
        ask_qty=40,
        bids=[BookLevel(99.5, 30), BookLevel(99.0, 100), BookLevel(98.5, 200)],
        asks=[BookLevel(100.5, 40), BookLevel(101.0, 80), BookLevel(101.5, 200)],
    )
    cover_buy = tick.cover_price(Side.BUY, 100)
    vwap, filled = tick.consume(Side.BUY, 100, limit=cover_buy)
    check("buy walks to the ask level that covers the size", cover_buy == 101.0, str(cover_buy))
    check("buy VWAP is between the first and second ask", 100.5 < vwap < 101.0, f"{vwap}")
    check("buy consumes the full quantity", filled == 100)
    cover_sell = tick.cover_price(Side.SELL, 50)
    sell_vwap, sell_qty = tick.consume(Side.SELL, 50, limit=cover_sell)
    check("sell walks down the bids", cover_sell == 99.0, str(cover_sell))
    check("sell VWAP is not the untouched top bid alone", sell_vwap < 99.5, f"{sell_vwap}")
    check("sell consumes the full quantity", sell_qty == 50)
    imb = tick.imbalance()
    check("more size on the bid is a positive imbalance", imb is not None and imb > 0, str(imb))


def test_market_hour_guards():
    wild = premium_triggers(120.0, 250.0)
    check("a wild ask does not fire the stop", wild == [120.0], str(wild))
    near = premium_triggers(168.0, 172.0)
    check("an ask close to the print still counts", max(near) == 172.0, str(near))
    cheap = premium_triggers(12.0, 9.5)
    check("a buy-back near the target still counts", min(cheap) == 9.5, str(cheap))

    store = TickStore()
    store.apply_ws_ticks([{
        "instrument_token": 1,
        "last_price": 100,
        "oi": 5000,
        "depth": {
            "buy": [{"price": 99.5, "quantity": 50}],
            "sell": [{"price": 100.5, "quantity": 50}],
        },
    }])
    store.apply_ws_ticks([{"instrument_token": 1, "last_price": 0, "oi": 0}])
    tick = store.get(1)
    check("a zero print does not wipe the last price", tick is not None and tick.ltp == 100, str(tick))
    check("a zero print does not wipe open interest", tick is not None and tick.oi == 5000)

    stale = prefer_snapshot(
        OrderSnapshot(order_id="1", status=OrderStatus.OPEN, filled_qty=0),
        OrderSnapshot(order_id="1", status=OrderStatus.COMPLETE, filled_qty=65, avg_price=120),
    )
    check("order history wins when the websocket missed the fill",
          stale.is_complete and stale.filled_qty == 65)

    fresh = prefer_snapshot(
        OrderSnapshot(order_id="1", status=OrderStatus.COMPLETE, filled_qty=65, avg_price=120),
        OrderSnapshot(order_id="1", status=OrderStatus.OPEN, filled_qty=0),
    )
    check("a finished websocket fill is not downgraded by a late history row",
          fresh.is_complete and fresh.filled_qty == 65)

    leg = Leg(
        tradingsymbol="NIFTY26OCT24500PE",
        token=1,
        exchange="NFO",
        strike=24500,
        opt_type=OptionType.PE,
        side=Side.SELL,
        lots=1,
        lot_size=65,
        role=LegRole.SHORT_PE,
        entry_price=100,
        exit_price=80,
        filled_qty=65,
        state=LegState.CLOSED,
    )
    # 65 contracts were closed earlier and booked here. The remaining 65 are on the leg.
    position = Position(
        id="POS1",
        underlying="NIFTY",
        strategy="BULL_PUT",
        expiry=date(2026, 10, 16),
        legs=[leg],
        state=PositionState.CLOSED,
        realised_pnl=(120 - 100) * 65,
    )
    check("partial exit rupees are kept once", position.total_pnl() == (20 * 65) + (20 * 65),
          str(position.total_pnl()))
    open_leg = Leg(
        tradingsymbol="NIFTY26OCT24500PE",
        token=2,
        exchange="NFO",
        strike=24500,
        opt_type=OptionType.PE,
        side=Side.SELL,
        lots=2,
        lot_size=65,
        role=LegRole.SHORT_PE,
        filled_qty=65,
        state=LegState.OPEN,
    )
    check("the next close orders only the remainder", open_leg.quantity == 65, str(open_leg.quantity))

    path = os.path.join(_TMP, "stale_state.json")
    from core import clock
    yesterday = (clock.today() - timedelta(days=1)).isoformat()
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"day": yesterday, "positions": [{"id": "POS1"}]}, handle)
    loaded = EngineStateStore(path).load()
    check("an unfinished spread from yesterday is kept",
          loaded.get("stale_day") is True and loaded.get("positions"), str(loaded.get("day")))


def test_analysis_clock():
    from core import clock
    from engine import cooldown_active
    check("09:25 is before the first print", clock.elapsed_seconds(at(9, 25)) is None)
    check(
        "09:15 is inside the session but not an OI print",
        clock.is_market_open(at(9, 15)) and clock.elapsed_seconds(at(9, 15)) is None,
    )
    check("the first print is 09:26", clock.elapsed_seconds(at(9, 26)) == 0)
    check("the first change is one minute later", clock.elapsed_seconds(at(9, 27)) == 60)
    check("09:40 is still inside the 15-minute analysis", clock.elapsed_seconds(at(9, 40)) == 14 * 60)
    check("09:41 is fifteen minutes after the open print", clock.elapsed_seconds(at(9, 41)) == 15 * 60)
    check("09:40 cannot trade", clock.hhmm("09:40") < clock.hhmm(config.SESSION["first_trade"]))
    check("09:41 is the first minute that can trade", config.SESSION["first_trade"] == "09:41")
    anchor = clock.analysis_start_dt(at(9, 41))
    check("buckets are anchored at 09:26", anchor == at(9, 26), str(anchor))
    check("15:15 is the square-off", clock.past_square_off(False, clock.hhmm("15:15")))
    check("15:14 is still inside the session", not clock.past_square_off(False, clock.hhmm("15:14")))
    from engine import baseline_ready
    full = snap(9, 26, 1_000_000, 2_000_000)
    check("09:26 with both ATM legs is a baseline", baseline_ready([full]))
    check("a series that starts at 09:27 is not a baseline", not baseline_ready([snap(9, 27, 1_000_000, 2_000_000)]))
    thin = snap(9, 26, 1_000_000, 2_000_000)
    thin.pe_by_strike.pop(24500)
    check("09:26 without the ATM put is not a baseline", not baseline_ready([thin]))
    totals = snap(9, 26, 1_000_000, 2_000_000)
    totals.ce_by_strike = {}
    totals.pe_by_strike = {}
    check("09:26 stored as ATM totals is still a baseline", baseline_ready([totals]))
    check("the flip minute is still inside the wait", cooldown_active(at(10, 0), at(10, 2)))
    check("two minutes later the next signal can be taken", not cooldown_active(at(10, 2), at(10, 2)))
    check("no wait is stored when nothing has flipped", not cooldown_active(at(10, 0), None))


def test_expiry_uses_today():
    from core import clock
    from data.instruments import InstrumentMaster
    master = InstrumentMaster.__new__(InstrumentMaster)
    today = clock.today()
    nxt = today + timedelta(days=6)
    later = today + timedelta(days=20)
    master._expiries = {"NIFTY": [today, nxt, later]}
    chosen = master.select_expiry("NIFTY")
    check("expiry day uses the contract expiring today", chosen == today, str(chosen))
    later_only = InstrumentMaster.__new__(InstrumentMaster)
    later_only._expiries = {"NIFTY": [nxt, later]}
    check(
        "a day that is not expiry uses the nearest weekly",
        later_only.select_expiry("NIFTY") == nxt,
        str(later_only.select_expiry("NIFTY")),
    )


def test_cache_stores_current_expiry_only():
    from core import clock
    from data.instruments import InstrumentMaster
    master = InstrumentMaster.__new__(InstrumentMaster)
    today = clock.today()
    weekly = today + timedelta(days=3)
    nxt = today + timedelta(days=6)
    monthly = today + timedelta(days=24)

    def row(expiry, kind):
        return {"name": "NIFTY", "expiry": expiry.isoformat(), "instrument_type": kind}

    later_weeks = [
        row(weekly, "CE"), row(weekly, "PE"), row(weekly, "FUT"),
        row(nxt, "CE"), row(monthly, "PE"),
    ]
    slim = master._current_expiry_only(later_weeks)
    kept = {(item["expiry"], item["instrument_type"]) for item in slim}
    check(
        "the state cache keeps only the current weekly call and put",
        kept == {(weekly.isoformat(), "CE"), (weekly.isoformat(), "PE")},
        str(sorted(kept)),
    )
    on_expiry = later_weeks + [row(today, "CE"), row(today, "PE"), row(today, "FUT")]
    expiry_day = master._current_expiry_only(on_expiry)
    kinds = {item["instrument_type"] for item in expiry_day}
    check(
        "on expiry day only today's call and put are stored",
        {item["expiry"] for item in expiry_day} == {today.isoformat()} and kinds == {"CE", "PE"},
        str(expiry_day),
    )


def test_config():
    problems = [p for p in config.validate() if "Telegram" not in p and "paper mode" not in p]
    check("config validates", not problems, "; ".join(problems))
    check("paper is the default", not config.is_live())
    check("only NIFTY is active", config.active_symbols() == ["NIFTY"])
    check("the tracked band is ATM ±7", config.SYMBOLS["NIFTY"]["chain_strikes"] == 7)
    check("first trade is 09:41", config.SESSION["first_trade"] == "09:41")
    check("fifteen analysis prints are required", config.ENGINE["min_open_samples"] == 15)
    check("last entry is 15:00", config.SESSION["last_entry"] == "15:00")
    check("analysis starts at 09:26", config.SESSION["analysis_start"] == "09:26")
    check("square-off is 15:15", config.SESSION["hard_square_off"] == "15:15")
    check("OI is read every minute", config.ENGINE["scan_minutes"] == 1)
    check("expiry day trades the contract expiring today", config.EXPIRY["allow_expiry_day"] is True)
    check("dominance needs more than 30%", config.OI["min_ratio"] == 0.30)
    check("the nearby strike needs more than 15%", config.OI["neighbor_ratio"] == 0.15)
    check("a flip waits 2 minutes", config.ENGINE["flip_cooldown_min"] == 2)
    check("a 5-minute spike is not an entry setting", "spike_multiple" not in config.OI and "spike_minutes" not in config.OI)
    check("speed is not an entry setting", "min_speed" not in config.OI and "spike_speed" not in config.OI)
    check("0 DTE sells a bid greater than 100", config.TRADE["zero_dte_min_premium"] == 100)
    check("telegram note is every 5 min", config.ENGINE["report_minutes"] == 5)
    check("stop is 50 points and target is premium 10",
          config.TRADE["sl_points"] == 50 and config.TRADE["target_premium"] == 10)


def test_zero_dte_short():
    atm = 24500.0
    step = 50.0
    rich = {24500.0: 140.0, 24550.0: 180.0}
    strike, note = choose_zero_dte_short(atm, step, OptionType.PE, rich)
    check("0 DTE sells ATM when its bid is above 100", strike == 24500.0 and note == "", str(strike))

    cheap_atm = {24500.0: 80.0, 24550.0: 95.0, 24600.0: 130.0, 24650.0: 160.0}
    strike, note = choose_zero_dte_short(atm, step, OptionType.PE, cheap_atm)
    check("0 DTE put walks to the first ITM bid above 100", strike == 24600.0, str(strike))

    call_bids = {24500.0: 70.0, 24450.0: 90.0, 24400.0: 110.0, 24350.0: 150.0}
    strike, note = choose_zero_dte_short(atm, step, OptionType.CE, call_bids)
    check("0 DTE call walks down, which is in the money", strike == 24400.0, str(strike))

    exact = {24500.0: 100.0, 24550.0: 100.0, 24600.0: 100.0, 24650.0: 100.0}
    strike, note = choose_zero_dte_short(atm, step, OptionType.PE, exact)
    check("a bid of exactly 100 is not enough", strike is None, note)

    missing = {24550.0: 200.0}
    strike, note = choose_zero_dte_short(atm, step, OptionType.PE, missing)
    check("a missing ATM bid does not sell ITM", strike is None, note)


def test_a_fast_stretch_does_not_decide():
    """A heavy last few minutes must not close or block a day that is still past 30%."""
    samples = walk(9, 15, 20, 1_000_000, 1_000_000, 100, 50_000)
    ce = samples[-1].ce_oi
    pe = samples[-1].pe_oi
    hour, minute = 9, 34
    for _ in range(5):
        minute += 1
        ce += 70_000
        samples.append(snap(hour, minute, ce, pe))
    read = combined_read(samples, samples[-1].at, at(9, 15))
    check("the day is still bullish after a fast call burst", read.bias is Bias.BULLISH, read.checks[0].detail)
    check("that burst does not close the put spread", not flip_ready(read, Bias.BULLISH))
    check("the day still qualifies on its own", entry_ready(read) and trade_bias(read) is Bias.BULLISH)


def main() -> int:
    print("NIFTY selling rules")
    test_expiry_uses_today()
    test_cache_stores_current_expiry_only()
    test_config()
    test_analysis_clock()
    test_oi()
    test_series_survives_restart()
    test_oi_stays_on_the_same_strikes()
    test_morning_trace_follows_the_strike()
    test_walls()
    test_dominance_alone()
    test_neighbor_on_the_line_does_not_open()
    test_a_fast_stretch_does_not_decide()
    test_zero_dte_short()
    test_combined_open()
    test_combined_reversal()
    test_hedge_pick()
    test_depth_walk()
    test_market_hour_guards()
    print(f"\n{PASSED} passed, {FAILED} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
