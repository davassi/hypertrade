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


def test_get_impact_prices_buy_is_ask_sell_is_bid(monkeypatch):
    """Hyperliquid's asset-ctx impactPxs = [impactBid, impactAsk]. A BUY must cross UP
    into the ASK and a SELL DOWN into the BID, so get_impact_prices must return
    (buy=ask, sell=bid). Bug: it returned them swapped (buy=bid, sell=ask), so an
    aggressive SELL was priced off the ASK and failed to cross the bid on wider-spread
    assets (the xyz:KR200 one-leg incident). _CountingPost ctx has impactPxs=[100,101]."""
    fake_post = _CountingPost()
    monkeypatch.setattr(
        "hypertrade.routes.hyperliquid_data_client.requests.post", fake_post
    )
    client = _client(monkeypatch)
    buy_impact, sell_impact = client.get_impact_prices("BTC")
    assert buy_impact == 101.0   # BUY crosses the ASK (higher of the two)
    assert sell_impact == 100.0  # SELL crosses the BID (lower of the two)


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


# ===================================================================
# HIP-3 builder-dex awareness: dex-qualified coins ("xyz:EWJ") route to the
# dex's metaAndAssetCtxs (with the `dex` field), cached per dex.
# ===================================================================

def test_dex_of_helper():
    assert HyperliquidDataClient._dex_of("xyz:EWJ") == "xyz"
    assert HyperliquidDataClient._dex_of("xyz:XYZ100") == "xyz"
    assert HyperliquidDataClient._dex_of("BTC") == ""


def test_get_meta_is_dex_aware(monkeypatch):
    """A dex-qualified coin fetches the dex's metaAndAssetCtxs (with the `dex`
    field) and a plain coin the main perp meta; each dex is cached once."""
    seen = []

    def _post(_url, json=None, timeout=None):  # noqa: A002 - mirror requests API
        seen.append(json)
        dex = (json or {}).get("dex")
        uni = (
            [{"name": "xyz:XYZ100", "szDecimals": 4, "maxLeverage": 30},
             {"name": "xyz:EWJ", "szDecimals": 3, "maxLeverage": 20}]
            if dex == "xyz" else
            [{"name": "BTC", "szDecimals": 3, "maxLeverage": 50}]
        )

        class _Resp:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> list:
                return [{"universe": uni}, [{} for _ in uni]]

        return _Resp()

    monkeypatch.setattr("hypertrade.routes.hyperliquid_data_client.requests.post", _post)
    client = _client(monkeypatch)

    assert client.get_meta("xyz:EWJ")["szDecimals"] == 3      # from the xyz dex universe
    assert client.get_meta("BTC")["maxLeverage"] == 50        # from the main universe
    metas = [p for p in seen if p.get("type") == "metaAndAssetCtxs"]
    assert {"type": "metaAndAssetCtxs", "dex": "xyz"} in metas
    assert {"type": "metaAndAssetCtxs"} in metas

    # cache is per-dex: a second xyz lookup adds no fetch
    before = len(metas)
    client.get_meta("xyz:XYZ100")
    after = len([p for p in seen if p.get("type") == "metaAndAssetCtxs"])
    assert after == before


def test_canonical_symbol_resolves_case_against_the_venue_universe(monkeypatch):
    """The universe is the only authority on a coin's spelling.

    Hyperliquid ships lower-case k-contracts (kFLOKI) alongside genuinely
    upper-case K names (KAITO) in the same universe, so no lexical rule can
    repair a corrupted spelling — only a lookup can. Resolution is dex-aware
    and reuses the per-dex meta cache, so it costs no extra round-trip on a
    path that already fetches meta.
    """
    def _post(_url, json=None, timeout=None):  # noqa: A002 - mirror requests API
        dex = (json or {}).get("dex")
        uni = (
            [{"name": "xyz:KR200", "szDecimals": 4, "maxLeverage": 30}]
            if dex == "xyz" else
            [{"name": "kFLOKI", "szDecimals": 0, "maxLeverage": 5},
             {"name": "KAITO", "szDecimals": 1, "maxLeverage": 10}]
        )

        class _Resp:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> list:
                return [{"universe": uni}, [{} for _ in uni]]

        return _Resp()

    monkeypatch.setattr(
        "hypertrade.routes.hyperliquid_data_client.requests.post", _post
    )
    client = _client(monkeypatch)

    assert client.canonical_symbol("KFLOKI") == "kFLOKI"
    assert client.canonical_symbol("kfloki") == "kFLOKI"
    assert client.canonical_symbol("KAITO") == "KAITO"
    assert client.canonical_symbol("xyz:kr200") == "xyz:KR200"


def test_canonical_symbol_propagates_a_venue_outage_as_retryable(monkeypatch):
    """A transport blip must stay a transport blip.

    Degrading to the lexical rule here looks harmless but is not: the best-effort
    spelling then reaches the exchange, and when it is wrong the venue answers
    'not found' — a HyperliquidValidationError, which the webhook retry loop
    treats as PERMANENT and refuses to retry. One dropped POST would turn an
    order that used to fill into a terminal reject, and on a CLOSE that strands
    an open position. Surfacing the network error keeps the request retryable.
    """
    def _raise(*_args, **_kwargs):
        raise requests.ConnectionError("connection refused")

    monkeypatch.setattr(
        "hypertrade.routes.hyperliquid_data_client.requests.post", _raise
    )
    client = _client(monkeypatch)

    with pytest.raises(HyperliquidNetworkError):
        client.canonical_symbol("kFLOKI")


def test_canonical_symbol_adds_no_round_trip_to_the_order_path(monkeypatch):
    """Resolution must reuse the memo the order path was going to populate anyway.

    place_order canonicalises and then immediately fetches mid/mark/meta. If
    resolution fetched its own copy of the universe it would add a network
    round-trip in front of every order.
    """
    posts = []

    def _post(_url, json=None, timeout=None):  # noqa: A002 - mirror requests API
        posts.append(json)

        class _Resp:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> list:
                uni = [{"name": "kFLOKI", "szDecimals": 0, "maxLeverage": 5}]
                return [{"universe": uni}, [{"markPx": "1", "midPx": "1"}]]

        return _Resp()

    monkeypatch.setattr(
        "hypertrade.routes.hyperliquid_data_client.requests.post", _post
    )
    client = _client(monkeypatch)

    symbol = client.canonical_symbol("KFLOKI")
    client.get_meta(symbol)

    assert symbol == "kFLOKI"
    assert len([p for p in posts if p.get("type") == "metaAndAssetCtxs"]) == 1
