"""Enum definitions used by the TradingView webhook route."""

from enum import Enum


class PositionType(str, Enum):
    """Current or previous position state reported by TradingView."""

    FLAT = "flat"
    LONG = "long"
    SHORT = "short"


class OrderAction(str, Enum):
    """Action associated with the order contained in the webhook."""

    BUY = "buy"
    SELL = "sell"


class SignalType(str, Enum):
    """Normalized trading signal derived from webhook payload state changes."""

    OPEN_LONG = "OPEN_LONG"
    CLOSE_LONG = "CLOSE_LONG"
    OPEN_SHORT = "OPEN_SHORT"
    CLOSE_SHORT = "CLOSE_SHORT"
    ADD_LONG = "ADD_LONG"
    REDUCE_LONG = "REDUCE_LONG"
    ADD_SHORT = "ADD_SHORT"
    REDUCE_SHORT = "REDUCE_SHORT"
    REVERSE_TO_LONG = "REVERSE_TO_LONG"
    REVERSE_TO_SHORT = "REVERSE_TO_SHORT"
    NO_ACTION = "NO_ACTION"


class Side(str, Enum):
    """Order side mapping used when placing orders in exchanges."""

    BUY = "buy"
    SELL = "sell"
