"""
data/chain.py
=============
Builds the option-chain snapshot the whole engine reasons about.

Prices come from the websocket (bid/ask/depth), not from REST polling, so the
chain is only as old as the last tick. REST `quote` is used for two things only:
seeding the cache at startup and repairing individual tokens that have gone
stale (an untraded strike never ticks).

The tracked window is ATM and seven strikes either side. Open interest on
that band is what the signal uses. Prices come off the websocket so a stop
or a target does not wait on REST. A quote is sent only when open interest
is missing or older than a minute. The cheap hedge, and an in-the-money
short on expiry day, are quoted when a trade is built.
"""

from __future__ import annotations

from typing import Dict, List, Optional

import config
from core import clock
from core.enums import Side
from core.logging_setup import get
from core.models import Chain, ChainRow
from data.instruments import InstrumentMaster, OptionInstrument
from data.kite_gateway import KiteGateway
from data.tick_store import TickStore
from data.ws_feed import WebSocketFeed

log = get("chain")

# Websocket full-mode packets already carry OI. Quote only what has gone quiet.
OI_FRESH_SEC = 45.0


class ChainBuilder:
    def __init__(
        self,
        gateway: KiteGateway,
        instruments: InstrumentMaster,
        ticks: TickStore,
        feed: Optional[WebSocketFeed] = None,
    ) -> None:
        self.gw = gateway
        self.instruments = instruments
        self.ticks = ticks
        self.feed = feed

        self._expiry: Dict[str, object] = {}
        self._window_tokens: Dict[str, List[int]] = {}
        self._token_index: Dict[str, Dict[int, OptionInstrument]] = {}
        self._last_chain: Dict[str, Chain] = {}
        self._seeded: Dict[str, bool] = {}

    # ─── Subscription window ──────────────────────────────────────────────────

    def tokens_for(self, symbol: str) -> List[int]:
        """Tokens the feed should carry for this symbol (chain window + spot)."""
        out = list(self._window_tokens.get(symbol, []))
        spot_token = self.instruments.spot_token(symbol)
        if spot_token:
            out.append(spot_token)
        return out

    def _refresh_window(self, symbol: str, expiry, atm: float) -> List[OptionInstrument]:
        each_side = config.symbol_cfg(symbol)["chain_strikes"]
        window = self.instruments.strike_window(symbol, expiry, atm, each_side)
        self._token_index[symbol] = {i.token: i for i in window}
        self._window_tokens[symbol] = [i.token for i in window]
        return window

    # ─── Spot ─────────────────────────────────────────────────────────────────

    def spot(self, symbol: str) -> float:
        token = self.instruments.spot_token(symbol)
        if token:
            price = self.ticks.ltp(token)
            if price > 0 and self.ticks.age(token) < 60:
                return price
        key = config.symbol_cfg(symbol)["spot_key"]
        row = self.gw.ltp([key]).get(key) or {}
        try:
            return float(row.get("last_price") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    # ─── Build ────────────────────────────────────────────────────────────────

    def build(self, symbol: str) -> Optional[Chain]:
        expiry = self.instruments.select_expiry(symbol)
        if expiry is None:
            log.warning("%s: no expiry inside the configured DTE window", symbol)
            return None

        spot_price = self.spot(symbol)
        if spot_price <= 0:
            log.error("%s: could not read spot", symbol)
            return None

        atm = self.instruments.atm_strike(symbol, expiry, spot_price)
        previous = set(self._window_tokens.get(symbol, []))
        window = self._refresh_window(symbol, expiry, atm)
        if not window:
            log.error("%s: no option instruments around ATM %s", symbol, atm)
            return None

        if self.feed is not None:
            self.feed.add(self.tokens_for(symbol))
            # As spot moves, the band moves with it. Tokens that left are
            # unsubscribed, unless a position still pins them. Their morning
            # open interest stays in the saved prints.
            dropped = previous - set(self._window_tokens.get(symbol, []))
            if dropped:
                self.feed.discard(dropped)

        self._seed_if_needed(symbol, window)
        self._refresh_oi(symbol, window)

        step = config.symbol_cfg(symbol)["strike_step"]
        chain = Chain(
            underlying=symbol,
            spot=spot_price,
            atm=atm,
            expiry=clock.as_date(expiry),
            strike_step=step,
        )

        for inst in window:
            tick = self.ticks.get(inst.token)
            if tick is None:
                continue

            row = ChainRow(
                token=inst.token,
                tradingsymbol=inst.tradingsymbol,
                exchange=inst.exchange,
                strike=inst.strike,
                opt_type=inst.opt_type,
                expiry=inst.expiry,
                lot_size=inst.lot_size,
                tick_size=inst.tick_size,
                ltp=tick.ltp,
                bid=tick.bid,
                ask=tick.ask,
                bid_depth=tick.depth_qty(Side.SELL),
                ask_depth=tick.depth_qty(Side.BUY),
                oi=tick.oi,
            )
            chain.rows.setdefault(inst.strike, {})[str(inst.opt_type)] = row

        self._last_chain[symbol] = chain
        self._expiry[symbol] = chain.expiry
        return chain

    # ─── Cache seeding / repair ───────────────────────────────────────────────

    def _quote_keys(self, instruments: List[OptionInstrument]) -> List[str]:
        return [f"{i.exchange}:{i.tradingsymbol}" for i in instruments]

    def _seed_if_needed(self, symbol: str, window: List[OptionInstrument]) -> None:
        """
        The websocket only pushes on change, so at startup a quiet strike has no
        tick at all. One REST quote sweep fills the whole window immediately.
        """
        if self._seeded.get(symbol):
            return
        keys = self._quote_keys(window)
        cfg = config.symbol_cfg(symbol)
        keys.append(cfg["spot_key"])
        quotes = self.gw.quote(keys)
        applied = self.ticks.apply_rest_quotes(quotes)
        log.info("%s: seeded %d instruments from REST quotes", symbol, applied)
        self._seeded[symbol] = applied > 0

    def _refresh_oi(self, symbol: str, window: List[OptionInstrument]) -> None:
        """
        One quote for the strikes whose open interest is missing or stale.

        A fresh websocket packet already has OI, so the common minute does
        not touch REST at all. A dead feed quotes the window once and returns.
        """
        stale: List[OptionInstrument] = []
        for inst in window:
            tick = self.ticks.get(inst.token)
            if tick is None or tick.oi <= 0 or tick.age_sec > OI_FRESH_SEC:
                stale.append(inst)
        if not stale:
            return
        quotes = self.gw.quote(self._quote_keys(stale))
        applied = self.ticks.apply_rest_quotes(quotes or {})
        if applied < len(stale):
            log.warning(
                "%s: OI quote filled %d of %d strikes — that minute may be skipped",
                symbol, applied, len(stale),
            )

    def reset_session(self) -> None:
        self._last_chain.clear()
        self._seeded.clear()
