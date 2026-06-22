"""Tests for cloid handling in the Hyperliquid execution client.

Two concerns:
  1. The execution client must convert the cloid STRING into the SDK's `Cloid`
     object before handing it to `exchange.order` (the SDK rejects a raw str).
  2. `find_order_by_cloid` must POST the exact `orderStatus` payload the SDK uses
     and distinguish "order found" from "not found", wrapping transport failures
     in the Hyperliquid error taxonomy so a query blip is retryable, not a 500.
"""

from __future__ import annotations

import pathlib
import sys
from unittest.mock import MagicMock, patch

import pytest
import requests

REPO_ROOT = str(pathlib.Path(__file__).resolve().parents[1])
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from hyperliquid.utils.types import Cloid

from hypertrade.routes.hyperliquid_execution_client import (
    HyperliquidExecutionClient,
    PositionSide,
)
from hypertrade.routes.hyperliquid_errors import (
    HyperliquidAPIError,
    HyperliquidNetworkError,
)

_VALID_CLOID = "0x" + "a" * 32


def _client(monkeypatch) -> HyperliquidExecutionClient:
    monkeypatch.setenv("HYPERTRADE_ENVIRONMENT", "test")
    monkeypatch.setenv("HYPERTRADE_MASTER_ADDR", "0xMASTER")
    monkeypatch.setenv("HYPERTRADE_API_WALLET_PRIV", "k")
    monkeypatch.setenv("HYPERTRADE_WEBHOOK_SECRET", "secret")
    from hypertrade.config import get_settings
    get_settings.cache_clear()
    # Build a real client but with the SDK Exchange and data client stubbed.
    with patch(
        "hypertrade.routes.hyperliquid_execution_client.Exchange"
    ), patch(
        "hypertrade.routes.hyperliquid_execution_client.HyperliquidDataClient"
    ):
        client = HyperliquidExecutionClient(
            private_key="0x" + "1" * 64,
            account_address="0xMASTER",
            vault_address=None,
            base_url="https://test",
        )
    # Deterministic aggressive price so we don't depend on impact data.
    client._aggressive_price_from_impact = MagicMock(return_value=100.0)
    client._normalize_price = MagicMock(return_value=100.0)
    return client


def test_market_order_converts_cloid_string_to_cloid_object(monkeypatch):
    """The SDK's exchange.order wants a Cloid object, not a raw string."""
    client = _client(monkeypatch)
    client.exchange.order.return_value = {"response": {"data": {"statuses": [{}]}}}

    client.market_order(
        symbol="SOL", side=PositionSide.LONG, size=1.0, cloid=_VALID_CLOID
    )

    args, kwargs = client.exchange.order.call_args
    # cloid is the 7th positional arg (coin, is_buy, sz, px, type, reduce_only, cloid)
    passed_cloid = args[6] if len(args) > 6 else kwargs.get("cloid")
    assert isinstance(passed_cloid, Cloid)
    assert passed_cloid.to_raw() == _VALID_CLOID


def test_market_order_none_cloid_passes_none(monkeypatch):
    """No cloid -> None reaches the SDK (not an empty Cloid)."""
    client = _client(monkeypatch)
    client.exchange.order.return_value = {"response": {"data": {"statuses": [{}]}}}

    client.market_order(symbol="SOL", side=PositionSide.LONG, size=1.0, cloid=None)

    args, kwargs = client.exchange.order.call_args
    passed_cloid = args[6] if len(args) > 6 else kwargs.get("cloid")
    assert passed_cloid is None


def test_find_order_by_cloid_found(monkeypatch):
    """A {"status": "order", ...} response means the order landed -> return it."""
    client = _client(monkeypatch)
    found = {"status": "order", "order": {"order": {"oid": 7}}}

    class _Resp:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return found

    captured = {}

    def _post(url, json=None, timeout=None):  # noqa: A002 - mirror requests.post sig
        captured["url"] = url
        captured["payload"] = json
        return _Resp()

    monkeypatch.setattr(
        "hypertrade.routes.hyperliquid_execution_client.requests.post", _post
    )

    result = client.find_order_by_cloid(_VALID_CLOID, user="0xMASTER")
    assert result == found
    # Exact SDK payload shape: orderStatus with oid=cloid raw string.
    assert captured["payload"] == {
        "type": "orderStatus",
        "user": "0xMASTER",
        "oid": _VALID_CLOID,
    }
    assert captured["url"].endswith("/info")


def test_find_order_by_cloid_not_found_returns_none(monkeypatch):
    """An "unknownOid" status means the order never landed -> None."""
    client = _client(monkeypatch)

    class _Resp:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"status": "unknownOid"}

    monkeypatch.setattr(
        "hypertrade.routes.hyperliquid_execution_client.requests.post",
        lambda *a, **k: _Resp(),
    )

    assert client.find_order_by_cloid(_VALID_CLOID, user="0xMASTER") is None


def test_find_order_by_cloid_network_error_is_translated(monkeypatch):
    """A transport failure during the query must surface as the taxonomy error,
    NOT a raw requests exception (so the caller treats it as unconfirmable)."""
    client = _client(monkeypatch)

    def _raise(*_a, **_k):
        raise requests.ConnectionError("dropped")

    monkeypatch.setattr(
        "hypertrade.routes.hyperliquid_execution_client.requests.post", _raise
    )

    with pytest.raises(HyperliquidNetworkError):
        client.find_order_by_cloid(_VALID_CLOID, user="0xMASTER")


def test_find_order_by_cloid_http_error_is_translated(monkeypatch):
    """A non-2xx during the query surfaces as HyperliquidAPIError."""
    client = _client(monkeypatch)

    class _Resp:
        def raise_for_status(self) -> None:
            raise requests.HTTPError("500")

        def json(self) -> dict:  # pragma: no cover
            return {}

    monkeypatch.setattr(
        "hypertrade.routes.hyperliquid_execution_client.requests.post",
        lambda *a, **k: _Resp(),
    )

    with pytest.raises(HyperliquidAPIError):
        client.find_order_by_cloid(_VALID_CLOID, user="0xMASTER")


def test_close_position_does_not_escalate_on_no_fill(monkeypatch):
    """TD-17: close_position submits ONE reduce-only IOC and does not retry with
    an escalated premium on a non-crossing IOC. Slippage-tolerance / aggressive-
    close policy belongs to the strategy bot, not this thin executor; the nested
    same-cloid retry also conflicted with cloid idempotency (TD-1). The non-fill
    response is returned to the caller as-is."""
    client = _client(monkeypatch)
    no_fill = {
        "response": {
            "data": {
                "statuses": [
                    {"error": "Order could not immediately match against any resting orders."}
                ]
            }
        }
    }
    client.exchange.order.return_value = no_fill

    result = client.close_position(
        symbol="SOL", side=PositionSide.LONG, size=1.0, cloid=_VALID_CLOID
    )

    assert client.exchange.order.call_count == 1  # exactly one submission, no escalation
    assert result == no_fill
