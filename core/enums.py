"""
core/enums.py
=============
String-valued enums so every state is JSON-serialisable without converters.
"""

from __future__ import annotations

from enum import Enum


class StrEnum(str, Enum):
    """str subclass enum — compares equal to its value and serialises cleanly."""

    def __str__(self) -> str:                      # pragma: no cover
        return str(self.value)


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def opposite(self) -> "Side":
        return Side.SELL if self is Side.BUY else Side.BUY

    @property
    def sign(self) -> int:
        """+1 for a long position, -1 for a short position."""
        return 1 if self is Side.BUY else -1


class OptionType(StrEnum):
    CE = "CE"
    PE = "PE"

    @property
    def opposite(self) -> "OptionType":
        return OptionType.PE if self is OptionType.CE else OptionType.CE


class Bias(StrEnum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"

    @property
    def opposite(self) -> "Bias":
        if self is Bias.BULLISH:
            return Bias.BEARISH
        if self is Bias.BEARISH:
            return Bias.BULLISH
        return Bias.NEUTRAL


class PositionState(StrEnum):
    PENDING_ENTRY = "PENDING_ENTRY"
    OPEN = "OPEN"
    ADJUSTING = "ADJUSTING"
    PENDING_EXIT = "PENDING_EXIT"
    CLOSED = "CLOSED"
    FAILED = "FAILED"


class LegState(StrEnum):
    PENDING = "PENDING"
    OPEN = "OPEN"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"
    REJECTED = "REJECTED"


class LegRole(StrEnum):
    SHORT_CE = "SHORT_CE"
    SHORT_PE = "SHORT_PE"
    HEDGE_CE = "HEDGE_CE"
    HEDGE_PE = "HEDGE_PE"
    ADJ_HEDGE_CE = "ADJ_HEDGE_CE"
    ADJ_HEDGE_PE = "ADJ_HEDGE_PE"
    ADJ_SHORT_CE = "ADJ_SHORT_CE"
    ADJ_SHORT_PE = "ADJ_SHORT_PE"


class ExitReason(StrEnum):
    TARGET = "TARGET"
    STOPLOSS = "STOPLOSS"
    TRAILING_STOP = "TRAILING_STOP"
    LEG_PANIC = "LEG_PANIC"
    MAX_LOSS = "MAX_LOSS"
    TIME_SQUARE_OFF = "TIME_SQUARE_OFF"
    LATE_DAY_BOOK = "LATE_DAY_BOOK"
    GAMMA_GUARD = "GAMMA_GUARD"
    STAGNATION = "STAGNATION"
    DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT"
    DAILY_PROFIT_LOCK = "DAILY_PROFIT_LOCK"
    STRATEGY_SWITCH = "STRATEGY_SWITCH"
    ADVERSE_FLIP = "ADVERSE_FLIP"
    ADJUSTMENT_EXIT = "ADJUSTMENT_EXIT"
    FEED_DEAD = "FEED_DEAD"
    MANUAL = "MANUAL"
    SHUTDOWN = "SHUTDOWN"

    @property
    def is_loss_event(self) -> bool:
        return self in (
            ExitReason.STOPLOSS,
            ExitReason.LEG_PANIC,
            ExitReason.MAX_LOSS,
            ExitReason.GAMMA_GUARD,
            ExitReason.ADVERSE_FLIP,
        )


class AdjustAction(StrEnum):
    HEDGE_TESTED = "HEDGE_TESTED"
    ROLL_TESTED_AWAY = "ROLL_TESTED_AWAY"
    ROLL_UNTESTED_IN = "ROLL_UNTESTED_IN"
    CONVERT_TO_SPREAD = "CONVERT_TO_SPREAD"
    FLIP_DIRECTION = "FLIP_DIRECTION"
    EXIT_ALL = "EXIT_ALL"


class OrderStatus(StrEnum):
    CREATED = "CREATED"
    OPEN = "OPEN"
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    ERROR = "ERROR"
