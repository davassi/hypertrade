"""Tests that the REST data client translates raw `requests` transport errors
into the Hyperliquid error taxonomy, so a network blip is retryable rather than
an unhandled 500.
"""

from __future__ import annotations

import pathlib
import sys

import pytest
import requests

REPO_ROOT = str(pathlib.Path(__file__).resolve().parents[1])
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from hypertrade.routes.hyperliquid_data_client import HyperliquidDataClient
from hypertrade.routes.hyperliquid_errors import (
    HyperliquidAPIError,
    HyperliquidNetworkError,
    HyperliquidValidationError,
)


def _client(monkeypatch) -> HyperliquidDataClient:
    """A data client built against test settings (no real network)."""
    monkeypatch.setenv("HYPERTRADE_ENVIRONMENT", "test")
    monkeypatch.setenv("HYPERTRADE_MASTER_ADDR", "0xM")
    monkeypatch.setenv("HYPERTRADE_API_WALLET_PRIV", "k")
    monkeypatch.setenv("HYPERTRADE_WEBHOOK_SECRET", "secret")
    from hypertrade.config import get_settings
    get_settings.cache_clear()
    return HyperliquidDataClient(account_address="0xM", base_url="https://test")


def test_connection_error_becomes_network_error(monkeypatch):
    """A dropped connection during a POST surfaces as HyperliquidNetworkError."""
    client = _client(monkeypatch)

    def _raise(*_args, **_kwargs):
        raise requests.ConnectionError("connection refused")

    monkeypatch.setattr(
        "hypertrade.routes.hyperliquid_data_client.requests.post", _raise
    )

    with pytest.raises(HyperliquidNetworkError):
        client.get_mid("BTC")


def test_http_error_becomes_api_error(monkeypatch):
    """A non-2xx response (raise_for_status) surfaces as HyperliquidAPIError."""
    client = _client(monkeypatch)

    class _Resp:
        def raise_for_status(self) -> None:
            raise requests.HTTPError("500 Server Error")

        def json(self) -> dict:  # pragma: no cover - never reached
            return {}

    monkeypatch.setattr(
        "hypertrade.routes.hyperliquid_data_client.requests.post",
        lambda *_a, **_k: _Resp(),
    )

    with pytest.raises(HyperliquidAPIError):
        client.get_mid("BTC")


def test_timeout_on_balance_becomes_network_error(monkeypatch):
    """A timeout on the balance endpoint is retryable → HyperliquidNetworkError."""
    client = _client(monkeypatch)

    def _raise(*_args, **_kwargs):
        raise requests.Timeout("read timed out")

    monkeypatch.setattr(
        "hypertrade.routes.hyperliquid_data_client.requests.post", _raise
    )

    with pytest.raises(HyperliquidNetworkError):
        client.get_available_balance("0xM")


class _CountingPost:
    """Fake `requests.post` that counts metaAndAssetCtxs POSTs and returns a
    realistic meta/asset-ctx payload.
    """

    def __init__(self) -> None:
        self.meta_calls = 0

    def __call__(self, _url, json=None, timeout=None):  # noqa: A002 - mirror requests API
        if json is not None and json.get("type") == "metaAndAssetCtxs":
            self.meta_calls += 1

        class _Resp:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> list:
                return [
                    {"universe": [{"name": "BTC", "szDecimals": 3, "maxLeverage": 50}]},
                    [{"impactPxs": ["100", "101"], "midPx": "100.5", "markPx": "100.4"}],
                ]

        return _Resp()


def test_meta_fetch_memoized_per_instance(monkeypatch):
    """Multiple meta/ctx getters on ONE instance trigger a single network fetch."""
    fake_post = _CountingPost()
    monkeypatch.setattr(
        "hypertrade.routes.hyperliquid_data_client.requests.post", fake_post
    )

    client = _client(monkeypatch)
    client.get_meta("BTC")
    client.get_impact_prices("BTC")
    client.get_mid("BTC")

    assert fake_post.meta_calls == 1


def test_meta_memo_is_per_instance_not_global(monkeypatch):
    """A fresh instance re-fetches; the memo does not leak across instances."""
    fake_post = _CountingPost()
    monkeypatch.setattr(
        "hypertrade.routes.hyperliquid_data_client.requests.post", fake_post
    )

    first = _client(monkeypatch)
    first.get_meta("BTC")
    assert fake_post.meta_calls == 1

    second = _client(monkeypatch)
    second.get_meta("BTC")
    assert fake_post.meta_calls == 2


# ===================================================================
# Unknown symbol -> HyperliquidValidationError (TD-2 (A))
# ===================================================================

_UNIVERSE = [
    {"name": "BTC", "szDecimals": 3, "maxLeverage": 50},
    {"name": "ETH", "szDecimals": 4, "maxLeverage": 50},
]


def test_symbol_to_idx_returns_index_for_present_symbol():
    """A symbol present in the universe resolves to its position."""
    assert HyperliquidDataClient._symbol_to_idx("BTC", _UNIVERSE) == 0
    assert HyperliquidDataClient._symbol_to_idx("ETH", _UNIVERSE) == 1


def test_symbol_to_idx_unknown_symbol_raises_validation_error():
    """An unknown ticker (typo/delisted) is bad client input → validation error,
    NOT a raw ValueError that would escape the taxonomy as an unhandled 500."""
    with pytest.raises(HyperliquidValidationError):
        HyperliquidDataClient._symbol_to_idx("NOPE", _UNIVERSE)


def test_symbol_to_idx_unknown_symbol_is_not_a_value_error():
    """The unknown-symbol error must NOT be a ValueError subclass — the webhook
    taxonomy handlers only catch HyperliquidError variants."""
    with pytest.raises(HyperliquidValidationError) as excinfo:
        HyperliquidDataClient._symbol_to_idx("NOPE", _UNIVERSE)
    assert not isinstance(excinfo.value, ValueError)


def test_get_meta_unknown_symbol_raises_validation_error(monkeypatch):
    """get_meta() for a ticker not on Hyperliquid raises HyperliquidValidationError
    (the path place_order() takes before trading)."""
    fake_post = _CountingPost()
    monkeypatch.setattr(
        "hypertrade.routes.hyperliquid_data_client.requests.post", fake_post
    )
    client = _client(monkeypatch)
    with pytest.raises(HyperliquidValidationError):
        client.get_meta("NOPE")
