"""Unit tests for HyperliquidClient and related dataclasses."""

from __future__ import annotations

import sys
import pathlib
from decimal import Decimal

import pytest
from pydantic import SecretStr

from hypertrade.routes.hyperliquid_service import (
    HyperliquidClient,
    OrderRequest,
    HyperliquidError,
)
from hypertrade.routes.tradingview_enums import Side 

def test_place_order_simple_mock_success() -> None:
    client = HyperliquidClient(mock=True, subaccount_addr="sub-default")
    result = client.place_order("SOL", Side.BUY, 1.25, None, subaccount="sub-1")
    assert result["status"] == "ok"
    assert result["symbol"] == "SOL"
    assert result["side"] == "buy"
    assert result["qty"] == "1.25"
    assert result["price"] is None
    assert result.get("order_id", "").startswith("mock-")

def test_place_order_validation_errors() -> None:
    client = HyperliquidClient(mock=True)
    # qty must be > 0
    with pytest.raises(HyperliquidError):
        client.place_order(OrderRequest(symbol="BTCUSD", side=Side.BUY, qty=Decimal("0")))
    # symbol required (non-empty)
    with pytest.raises(HyperliquidError):
        client.place_order(OrderRequest(symbol="  ", side=Side.BUY, qty=Decimal("1")))


def test_non_mock_raises_not_implemented() -> None:
    client = HyperliquidClient(mock=False)
    with pytest.raises(HyperliquidError) as excinfo:
        client.place_order(OrderRequest(symbol="BTCUSD", side=Side.SELL, qty=Decimal("1")))
    assert "not implemented" in str(excinfo.value).lower()


def test_order_request_defaults() -> None:
    req = OrderRequest(symbol="SOLUSD", side=Side.SELL, qty=Decimal("2"))
    assert req.price is None
    assert req.reduce_only is False
    assert req.post_only is False
    assert req.client_id is None
    assert req.leverage is None
    assert req.subaccount is None

