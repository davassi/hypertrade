from enum import Enum

# Enums for tradingview route
class PositionType(str, Enum):
    FLAT = "flat"
    LONG = "long"
    SHORT = "short"

# Enums for order actions
class OrderAction(str, Enum):
    BUY = "buy"
    SELL = "sell"

# Enums for signal types
class SignalType(str, Enum):
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

# Enums for order sides
class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"
