"""Tests for `_build_response`: the /webhook response must surface the
actual fill (total_size/avg_price) alongside the requested `contracts`,
not just echo the request back.
"""

from __future__ import annotations

import copy

from hypertrade.routes.webhooks import _build_response
from hypertrade.routes.tradingview_enums import Side, SignalType
from hypertrade.schemas.tradingview import TradingViewWebhook

# Mirrors tests/test_webhook.py::BASE_PAYLOAD — the repo's canonical
# TradingViewWebhook payload shape used across webhook tests.
BASE_PAYLOAD = {
    "general": {
        "strategy": "Solana Super Cool Enhanced Strategy (114, 21, 1, 2, 0)",
        "ticker": "SOLUSD",
        "interval": "60",
        "time": "2025-10-21T06:00:00Z",
        "timenow": "2025-10-21T06:00:45Z",
        "secret": "secret",
        "leverage": "1X",
    },
    "currency": {"quote": "USD", "base": "SOL"},
    "order": {
        "action": "buy",
        "contracts": "46231.75300000",
        "price": "183.81",
        "id": "Short Exit",
        "comment": "Short Exit",
        "alert_message": "",
    },
    "market": {
        "position": "flat",
        "position_size": "0",
        "previous_position": "short",
        "previous_position_size": "46231.75300000",
    },
}


def make_payload(*, contracts: str, price: str, ticker: str, action: str) -> TradingViewWebhook:
    """Build a real TradingViewWebhook model, following the same
    TradingViewWebhook.model_validate(...) pattern already used in
    tests/test_webhook.py::test_general_model_parses_nonce.
    """
    raw = copy.deepcopy(BASE_PAYLOAD)
    raw["general"]["ticker"] = ticker
    raw["order"]["contracts"] = contracts
    raw["order"]["price"] = price
    raw["order"]["action"] = action
    return TradingViewWebhook.model_validate(raw)


def test_build_response_includes_actual_fill():
    payload = make_payload(contracts="0.4151", price="1209.2", ticker="xyz:KR200", action="sell")
    result = {"orderId": 1, "totalSz": "0.01", "avgPx": "1205.6"}
    body = _build_response(payload, signal=SignalType.OPEN_SHORT, side=Side.SELL, symbol="xyz:KR200", result=result)
    assert body["total_size"] == "0.01"
    assert body["avg_price"] == "1205.6"
    assert body["contracts"] == "0.4151"   # requested, unchanged


def test_build_response_nulls_fill_when_absent():
    payload = make_payload(contracts="0.4151", price="1209.2", ticker="xyz:KR200", action="sell")
    body = _build_response(payload, signal=SignalType.OPEN_SHORT, side=Side.SELL, symbol="xyz:KR200", result=None)
    assert body["total_size"] is None
    assert body["avg_price"] is None
