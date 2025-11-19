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

