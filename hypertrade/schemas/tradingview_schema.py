"""JSON schema describing TradingView webhook payloads."""

TRADINGVIEW_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": True,
    "required": ["general", "symbol_data", "currency", "order", "market"],
    "properties": {
        "general": {
            "type": "object",
            "required": ["ticker", "exchange", "interval", "time", "timenow"],
            "properties": {
                "strategy": {"type": "string", "minLength": 1},
                "ticker": {"type": "string", "minLength": 1},
                "exchange": {"type": "string", "minLength": 1},
                "interval": {"type": "string", "minLength": 1},
                "time": {"type": "string", "format": "date-time"},
                "timenow": {"type": "string", "format": "date-time"},
                "secret": {"type": "string", "minLength": 1},
                "leverage": {"type": "string", "minLength": 1}
            },
            "additionalProperties": True
        },
        "symbol_data": {
            "type": "object",
            "required": ["open", "close", "high", "low", "volume"],
            "properties": {
                "open": {"type": ["string", "number"]},
                "close": {"type": ["string", "number"]},
                "high": {"type": ["string", "number"]},
                "low": {"type": ["string", "number"]},
                "volume": {"type": ["string", "number"]}
            },
            "additionalProperties": True
        },
        "currency": {
            "type": "object",
            "required": ["quote", "base"],
            "properties": {
                "quote": {"type": "string", "minLength": 1},
                "base": {"type": "string", "minLength": 1}
            },
            "additionalProperties": True
        },
        "position": {
            "type": "object",
            "required": ["position_size"],
            "properties": {
                "position_size": {"type": ["string", "number"]}
            },
            "additionalProperties": True
        },
        "order": {
            "type": "object",
            "required": ["action", "contracts", "price", "id"],
            "properties": {
                "action": {"type": "string", "enum": ["buy", "sell"]},
                "contracts": {"type": ["string", "number"]},
                "price": {"type": ["string", "number"]},
                "id": {"type": "string", "minLength": 1},
                "comment": {"type": ["string", "null"]},
                "alert_message": {"type": ["string", "null"]}
            },
            "additionalProperties": True
        },
        "market": {
            "type": "object",
            "required": [
                "position",
                "position_size",
                "previous_position",
                "previous_position_size",
            ],
            "properties": {
                "position": {"type": "string", "enum": ["long", "short", "flat"]},
                "position_size": {"type": ["string", "number"]},
                "previous_position": {"type": "string", "enum": ["long", "short", "flat"]},
                "previous_position_size": {"type": ["string", "number"]}
            },
            "additionalProperties": True
        }
    }
}
