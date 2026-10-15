"""
strategies/spread.py
====================
Builds the only two trades this bot takes.

    BULLISH  sell the ATM put,  hedged by the nearest OTM put  under ₹10
    BEARISH  sell the ATM call, hedged by the nearest OTM call under ₹10

On the day the contract expires, ATM is sold only when its bid is greater
than ₹100. Otherwise the first of ITM1, ITM2, ITM3 with a bid above ₹100
is sold. The hedge is still the nearest further-OTM wing under ₹10.

The hedge is bought first by the order router. Its strike is chosen by walking
out from ATM until the ask (the price we actually pay) is under the configured
premium. That wing is often far from the at-the-money strike.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

import config
from core import clock
from core.enums import Bias, LegRole, OptionType, Side
from core.logging_setup import get
from core.models import BookLevel, LegSpec, StrategyPlan, Tick
from data.instruments import InstrumentMaster, OptionInstrument
from data.kite_gateway import KiteGateway
from data.tick_store import TickStore

log = get("spread")


def pick_hedge(
    ladder: List[Tuple[float, float]],
    max_premium: float,
    min_premium: float,
) -> Optional[float]:
    """
    `ladder` is (strike, ask) walking away from ATM, nearest first.

    The first ask that is strictly below `max_premium` wins. That is the
    tightest wing that still costs less than the hedge cap.
    """
    for strike, ask in ladder:
        if ask <= 0:
            continue
        if min_premium <= ask < max_premium:
            return strike
    return None


def choose_zero_dte_short(
    atm: float,
    step: float,
    opt_type: OptionType,
    bids: Dict[float, float],
) -> Tuple[Optional[float], str]:
    """
    On 0 DTE, sell ATM when its bid is greater than the floor.

    Otherwise walk ITM1, ITM2, ITM3 and sell the first bid above that floor.
    A put's in-the-money strikes are above ATM. A call's are below ATM.
    A missing or zero ATM bid does not fall through to an ITM strike.
    """
    floor = float(config.TRADE["zero_dte_min_premium"])
    steps = int(config.TRADE["zero_dte_itm_steps"])
    direction = 1 if opt_type is OptionType.PE else -1
    atm_bid = float(bids.get(float(atm), 0) or 0)
    if atm_bid <= 0:
        return None, f"0 DTE ATM {int(atm)} {opt_type} has no bid, so ITM strikes are not used"
    if atm_bid > floor:
        return float(atm), ""

    for index in range(1, steps + 1):
        strike = float(atm + direction * index * step)
        bid = float(bids.get(strike, 0) or 0)
        if bid > floor:
            return strike, ""
    shown = ", ".join(
        f"ITM{index} {int(atm + direction * index * step)} "
        f"bid {float(bids.get(float(atm + direction * index * step), 0) or 0):.2f}"
        for index in range(1, steps + 1)
    )
    return None, (
        f"0 DTE ATM {int(atm)} {opt_type} bid is {atm_bid:.2f}, "
        f"and no ITM bid is above ₹{floor:.0f} ({shown})"
    )


def build_plan(
    instruments: InstrumentMaster,
    ticks: TickStore,
    gateway: KiteGateway,
    symbol: str,
    bias: Bias,
    spot: float,
    reasons: Optional[List[str]] = None,
) -> Tuple[Optional[StrategyPlan], str]:
    """
    Return (plan, note). `plan` is None when the book cannot build the spread,
    and `note` says why in one sentence.
    """
    if bias not in (Bias.BULLISH, Bias.BEARISH):
        return None, "the hour is not bullish or bearish"

    expiry = instruments.select_expiry(symbol)
    if expiry is None:
        return None, "no NIFTY expiry in the configured window"
    atm = instruments.atm_strike(symbol, expiry, spot)
    opt_type = OptionType.PE if bias is Bias.BULLISH else OptionType.CE
    role_short = LegRole.SHORT_PE if bias is Bias.BULLISH else LegRole.SHORT_CE
    role_hedge = LegRole.HEDGE_PE if bias is Bias.BULLISH else LegRole.HEDGE_CE

    short, short_bid, short_note = _select_short(
        instruments, ticks, gateway, symbol, expiry, atm, opt_type
    )
    if short is None:
        return None, short_note

    hedge, hedge_ask, hedge_note = _find_hedge(
        instruments, ticks, gateway, symbol, expiry, short.strike, opt_type
    )
    if hedge is None:
        return None, hedge_note

    lots = int(config.TRADE["lots"])
    lot_size = instruments.lot_size(symbol)
    name = "BULL_PUT" if bias is Bias.BULLISH else "BEAR_CALL"
    legs = [
        _spec(hedge, Side.BUY, lots, lot_size, role_hedge, hedge_ask),
        _spec(short, Side.SELL, lots, lot_size, role_short, short_bid),
    ]
    credit = short_bid - hedge_ask
    width = abs(short.strike - hedge.strike)
    plan = StrategyPlan(
        strategy=name,
        underlying=symbol,
        expiry=expiry,
        legs=legs,
        lots=lots,
        lot_size=lot_size,
        credit_per_unit=credit,
        score=0.0,
        reasons=list(reasons or []),
        max_loss_per_unit=max(0.0, width - credit),
        meta={
            "bias": str(bias),
            "hedge_ask": hedge_ask,
            "short_bid": short_bid,
            "dte": clock.dte(expiry),
            "short_depth": _depth_line(ticks.get(short.token), Side.SELL),
            "hedge_depth": _depth_line(ticks.get(hedge.token), Side.BUY),
        },
    )
    log.info(
        "%s plan %s | sell %s @ %.2f | buy %s @ %.2f",
        symbol, name, short.tradingsymbol, short_bid, hedge.tradingsymbol, hedge_ask,
    )
    return plan, ""


def _spec(
    inst: OptionInstrument,
    side: Side,
    lots: int,
    lot_size: int,
    role: LegRole,
    ref_price: float,
) -> LegSpec:
    return LegSpec(
        tradingsymbol=inst.tradingsymbol,
        token=inst.token,
        exchange=inst.exchange,
        strike=inst.strike,
        opt_type=inst.opt_type,
        side=side,
        lots=lots,
        lot_size=lot_size,
        role=role,
        tick_size=inst.tick_size,
        ref_price=ref_price,
    )


def _select_short(
    instruments: InstrumentMaster,
    ticks: TickStore,
    gateway: KiteGateway,
    symbol: str,
    expiry,
    atm: float,
    opt_type: OptionType,
) -> Tuple[Optional[OptionInstrument], float, str]:
    """ATM on a normal day. On 0 DTE, ATM if the bid is above ₹100, else ITM1–ITM3."""
    step = float(config.symbol_cfg(symbol)["strike_step"])
    if clock.dte(expiry) == 0:
        return _select_zero_dte_short(
            instruments, ticks, gateway, symbol, expiry, atm, step, opt_type
        )

    short = instruments.option(symbol, expiry, atm, opt_type)
    if short is None:
        return None, 0.0, f"ATM {int(atm)} {opt_type} is not in the instrument dump"
    _refresh_bids(ticks, gateway, [short])
    bid = _sell_price(ticks.get(short.token))
    floor = float(config.TRADE["min_short_premium"])
    if bid < floor:
        shown = f"{bid:.2f}" if bid else "no bid"
        return None, 0.0, (
            f"ATM {int(atm)} {opt_type} bid is {shown}, below the {floor:.0f} minimum"
        )
    return short, bid, ""


def _select_zero_dte_short(
    instruments: InstrumentMaster,
    ticks: TickStore,
    gateway: KiteGateway,
    symbol: str,
    expiry,
    atm: float,
    step: float,
    opt_type: OptionType,
) -> Tuple[Optional[OptionInstrument], float, str]:
    direction = 1 if opt_type is OptionType.PE else -1
    steps = int(config.TRADE["zero_dte_itm_steps"])
    strikes = [float(atm + direction * index * step) for index in range(0, steps + 1)]
    contracts = []
    for strike in strikes:
        inst = instruments.option(symbol, expiry, strike, opt_type)
        if inst is not None:
            contracts.append(inst)
    if not contracts:
        return None, 0.0, f"0 DTE ATM {int(atm)} {opt_type} is not in the instrument dump"
    _refresh_bids(ticks, gateway, contracts)
    bids = {inst.strike: _sell_price(ticks.get(inst.token)) for inst in contracts}
    chosen, note = choose_zero_dte_short(atm, step, opt_type, bids)
    if chosen is None:
        return None, 0.0, note
    short = next(inst for inst in contracts if inst.strike == chosen)
    return short, bids[short.strike], ""


def _refresh_bids(
    ticks: TickStore,
    gateway: KiteGateway,
    contracts: List[OptionInstrument],
) -> None:
    """One quote for the strikes that have no sellable bid yet."""
    missing = [inst for inst in contracts if _sell_price(ticks.get(inst.token)) <= 0]
    if not missing:
        return
    quotes = gateway.quote([f"{inst.exchange}:{inst.tradingsymbol}" for inst in missing])
    ticks.apply_rest_quotes(quotes)


def _find_hedge(
    instruments: InstrumentMaster,
    ticks: TickStore,
    gateway: KiteGateway,
    symbol: str,
    expiry,
    origin: float,
    opt_type: OptionType,
) -> Tuple[Optional[OptionInstrument], float, str]:
    step = config.symbol_cfg(symbol)["strike_step"]
    direction = -1 if opt_type is OptionType.PE else 1
    steps = int(config.TRADE["hedge_search_steps"])
    candidates: List[OptionInstrument] = []
    for i in range(1, steps + 1):
        strike = round((origin + direction * i * step) / step) * step
        inst = instruments.option(symbol, expiry, float(strike), opt_type)
        if inst is not None:
            candidates.append(inst)
    if not candidates:
        return None, 0.0, "no OTM strikes to search for a hedge"

    missing = [inst for inst in candidates if _buy_price(ticks.get(inst.token)) <= 0]
    if missing:
        quotes = gateway.quote([f"{inst.exchange}:{inst.tradingsymbol}" for inst in missing])
        ticks.apply_rest_quotes(quotes)

    ladder: List[Tuple[float, float]] = []
    for inst in candidates:
        ask = _buy_price(ticks.get(inst.token))
        ladder.append((inst.strike, ask))

    chosen = pick_hedge(
        ladder,
        config.TRADE["hedge_max_premium"],
        config.TRADE["hedge_min_premium"],
    )
    if chosen is None:
        cap = config.TRADE["hedge_max_premium"]
        return None, 0.0, f"no {opt_type} wing with ask under ₹{cap:.0f} within {steps} strikes"

    inst = next(item for item in candidates if item.strike == chosen)
    return inst, _buy_price(ticks.get(inst.token)), ""


def _sell_price(tick: Optional[Tick]) -> float:
    """What a sell would hit: the bid that covers one lot, else the best bid."""
    if tick is None:
        return 0.0
    if tick.bid > 0:
        return tick.bid
    return tick.ltp


def _buy_price(tick: Optional[Tick]) -> float:
    """What a buy would lift: the best ask. Zero when the offer is missing."""
    if tick is None:
        return 0.0
    if tick.ask > 0:
        return tick.ask
    return 0.0


def _depth_line(tick: Optional[Tick], side: Side) -> str:
    if tick is None:
        return "no book"
    if side is Side.BUY:
        top = tick.ask
        top_qty = tick.ask_qty
        book = tick.depth_qty(Side.BUY)
        ladder = tick.asks
        verb = "lift asks"
    else:
        top = tick.bid
        top_qty = tick.bid_qty
        book = tick.depth_qty(Side.SELL)
        ladder = tick.bids
        verb = "hit bids"
    levels = _level_text(ladder)
    return (
        f"{verb} · top {top:.2f} × {top_qty:,} · "
        f"displayed {book:,}"
        + (f" · {levels}" if levels else "")
    )


def _level_text(levels: List[BookLevel]) -> str:
    parts = [f"{level.price:.2f}×{level.quantity:,}" for level in levels[:3]]
    return " ".join(parts)
