"""Tests for HyperliquidService.place_order — the real execution path (order
sizing and reduce_only forwarding) that the webhook suite stubs out entirely.
"""

from __future__ import annotations

import pathlib
import sys
from decimal import Decimal
from unittest.mock import patch

REPO_ROOT = str(pathlib.Path(__file__).resolve().parents[1])
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import pytest

from hypertrade.routes.hyperliquid_service import HyperliquidService, OrderRequest
from hypertrade.routes.hyperliquid_errors import HyperliquidAPIError, HyperliquidValidationError, HyperliquidRejection
from hypertrade.routes.tradingview_enums import Side, SignalType

_FILLED = {"response": {"data": {"statuses": [{"filled": {"avgPx": "100", "totalSz": "1"}}]}}}


def _service(monkeypatch):
    """A HyperliquidService whose execution client is fully mocked (no network)."""
    # place_order() reads get_settings() for the market-order premium; satisfy the
    # required settings and clear the lru_cache so this test's env is the one used.
    monkeypatch.setenv("HYPERTRADE_ENVIRONMENT", "test")
    monkeypatch.setenv("HYPERTRADE_MASTER_ADDR", "0xM")
    monkeypatch.setenv("HYPERTRADE_API_WALLET_PRIV", "k")
    monkeypatch.setenv("HYPERTRADE_WEBHOOK_SECRET", "secret")
    from hypertrade.config import get_settings
    get_settings.cache_clear()
    with patch("hypertrade.routes.hyperliquid_service.HyperliquidExecutionClient"):
        svc = HyperliquidService(base_url="https://test", master_addr="0xM", api_wallet_priv="k")
    client = svc.client
    client.data.get_mid.return_value = 100.0
    client.data.get_mark.return_value = 100.0
    client.data.get_meta.return_value = {"maxLeverage": 10, "szDecimals": 3}
    client.data.get_available_balance.return_value = 10_000.0
    client.update_leverage.return_value = {"status": "ok"}
    client.market_order.return_value = _FILLED
    client.close_position.return_value = _FILLED
    return svc, client


def test_open_order_size_is_qty_not_qty_times_leverage(monkeypatch):
    """Order size must equal the requested contracts, not contracts * leverage.

    Leverage is applied separately on the exchange via update_leverage; folding it
    into the order size as well would double-apply leverage (exposure = leverage^2).
    """
    svc, client = _service(monkeypatch)
    svc.place_order(OrderRequest(
        symbol="SOL", side=Side.BUY, signal=SignalType.OPEN_LONG,
        qty=Decimal("2"), price=Decimal("100"), leverage=5,
    ))
    _, kwargs = client.market_order.call_args
    assert kwargs["size"] == Decimal("2")          # NOT Decimal("10")
    client.update_leverage.assert_called_once()     # leverage still set on the exchange


def test_reduce_signal_forwards_reduce_only_to_market_order(monkeypatch):
    """REDUCE_* signals must reach the exchange as reduce-only orders so they can
    only shrink an existing position, never open an opposing one."""
    svc, client = _service(monkeypatch)
    svc.place_order(OrderRequest(
        symbol="SOL", side=Side.SELL, signal=SignalType.REDUCE_LONG,
        qty=Decimal("1"), price=Decimal("100"), leverage=1, reduce_only=True,
    ))
    _, kwargs = client.market_order.call_args
    assert kwargs.get("reduce_only") is True


def test_cloid_forwarded_to_market_order(monkeypatch):
    """The OrderRequest.cloid must be threaded down to the execution client so the
    submitted order carries the deterministic client order id."""
    svc, client = _service(monkeypatch)
    svc.place_order(OrderRequest(
        symbol="SOL", side=Side.BUY, signal=SignalType.OPEN_LONG,
        qty=Decimal("1"), price=Decimal("100"), leverage=1,
        cloid="0x" + "a" * 32,
    ))
    _, kwargs = client.market_order.call_args
    assert kwargs.get("cloid") == "0x" + "a" * 32


def test_cloid_forwarded_to_close_position(monkeypatch):
    """Closing orders must also carry the cloid for idempotent resubmission."""
    svc, client = _service(monkeypatch)
    svc.place_order(OrderRequest(
        symbol="SOL", side=Side.SELL, signal=SignalType.CLOSE_LONG,
        qty=Decimal("1"), price=Decimal("100"), leverage=1,
        cloid="0x" + "b" * 32,
    ))
    _, kwargs = client.close_position.call_args
    assert kwargs.get("cloid") == "0x" + "b" * 32


# ===================================================================
# Malformed (non-None) order response -> HyperliquidAPIError (TD-2 (B))
# ===================================================================

def test_happy_path_returns_well_formed_filled_response(monkeypatch):
    """The existing happy path (a well-formed filled response) still returns res."""
    svc, client = _service(monkeypatch)
    res = svc.place_order(OrderRequest(
        symbol="SOL", side=Side.BUY, signal=SignalType.OPEN_LONG,
        qty=Decimal("1"), price=Decimal("100"), leverage=1,
    ))
    assert res is _FILLED


def test_malformed_market_order_response_raises_api_error(monkeypatch):
    """A non-None but malformed exchange response must surface as
    HyperliquidAPIError (taxonomy), not a raw KeyError that escapes to a 500."""
    svc, client = _service(monkeypatch)
    client.market_order.return_value = {"unexpected": 1}
    with pytest.raises(HyperliquidAPIError):
        svc.place_order(OrderRequest(
            symbol="SOL", side=Side.BUY, signal=SignalType.OPEN_LONG,
            qty=Decimal("1"), price=Decimal("100"), leverage=1,
        ))


def test_malformed_close_position_response_raises_api_error(monkeypatch):
    """The close path must apply the same defensive parsing as the open path."""
    svc, client = _service(monkeypatch)
    client.close_position.return_value = {"unexpected": 1}
    with pytest.raises(HyperliquidAPIError):
        svc.place_order(OrderRequest(
            symbol="SOL", side=Side.SELL, signal=SignalType.CLOSE_LONG,
            qty=Decimal("1"), price=Decimal("100"), leverage=1,
        ))


# ===================================================================
# find_order_by_cloid: query the exchange for an already-placed order
# ===================================================================

def test_find_order_by_cloid_returns_order_when_found(monkeypatch):
    """When the exchange reports {"status": "order", ...} the method returns the
    payload so the caller can avoid a duplicate submission."""
    svc, client = _service(monkeypatch)
    found = {"status": "order", "order": {"order": {"oid": 99}}}
    client.find_order_by_cloid.return_value = found
    # Delegate through the service to the execution client.
    svc.client.find_order_by_cloid.return_value = found
    result = svc.find_order_by_cloid("0x" + "c" * 32)
    assert result == found


def test_find_order_by_cloid_returns_none_when_absent(monkeypatch):
    """When the exchange reports it does not know the order, return None."""
    svc, client = _service(monkeypatch)
    client.find_order_by_cloid.return_value = None
    result = svc.find_order_by_cloid("0x" + "d" * 32)
    assert result is None


# ===================================================================
# A statuses[0] {"error": ...} must surface, not look like a success
# (e.g. "Order has invalid price.", insufficient margin). Previously this
# fell into the else-branch, was logged as "Order submitted" and returned
# 200/ok — a phantom fill that desyncs the strategy bot.
# ===================================================================

def test_exchange_error_status_raises_rejection(monkeypatch):
    """An exchange-level error in statuses[0] must surface as a HyperliquidRejection (terminal after
    one retry → HTTP 400 → fast pause), never be reported as a placed order."""
    svc, client = _service(monkeypatch)
    client.market_order.return_value = {
        "response": {"data": {"statuses": [{"error": "Order has invalid price."}]}}
    }
    with pytest.raises(HyperliquidRejection):
        svc.place_order(OrderRequest(
            symbol="SOL", side=Side.BUY, signal=SignalType.OPEN_LONG,
            qty=Decimal("1"), price=Decimal("100"), leverage=1,
        ))


def test_exchange_error_status_on_close_raises_rejection(monkeypatch):
    """The close path shares the same status interpretation, so an error there
    must surface too (as a HyperliquidRejection)."""
    svc, client = _service(monkeypatch)
    client.close_position.return_value = {
        "response": {"data": {"statuses": [{"error": "insufficient margin"}]}}
    }
    with pytest.raises(HyperliquidRejection):
        svc.place_order(OrderRequest(
            symbol="SOL", side=Side.SELL, signal=SignalType.CLOSE_LONG,
            qty=Decimal("1"), price=Decimal("100"), leverage=1,
        ))


def test_resting_status_returns_without_error(monkeypatch):
    """A resting (accepted, not-yet-filled) status is a valid non-error outcome and
    must be returned, not mistaken for an error."""
    svc, client = _service(monkeypatch)
    resting = {"response": {"data": {"statuses": [{"resting": {"oid": 1}}]}}}
    client.market_order.return_value = resting
    res = svc.place_order(OrderRequest(
        symbol="SOL", side=Side.BUY, signal=SignalType.OPEN_LONG,
        qty=Decimal("1"), price=Decimal("100"), leverage=1,
    ))
    assert res is resting


# ===================================================================
# Symbol case: dex-qualified (HIP-3) coins keep their lowercase prefix.
# Uppercasing "xyz:NVDA" -> "XYZ:NVDA" breaks the dex meta lookup
# (metaAndAssetCtxs dex="XYZ" 500s) and name_to_asset.
# ===================================================================

def test_dex_qualified_symbol_case_is_preserved(monkeypatch):
    """A dex-qualified coin keeps its lowercase dex prefix through place_order, into
    both the meta lookup and the order submitted to the exchange."""
    svc, client = _service(monkeypatch)
    svc.place_order(OrderRequest(
        symbol="xyz:NVDA", side=Side.BUY, signal=SignalType.OPEN_LONG,
        qty=Decimal("1"), price=Decimal("100"), leverage=1,
    ))
    client.data.get_meta.assert_called_with("xyz:NVDA")
    _, kwargs = client.market_order.call_args
    assert kwargs["symbol"] == "xyz:NVDA"


def test_plain_symbol_is_uppercased(monkeypatch):
    """A plain (non-dex) coin is still normalized to uppercase (eth -> ETH)."""
    svc, client = _service(monkeypatch)
    svc.place_order(OrderRequest(
        symbol="eth", side=Side.BUY, signal=SignalType.OPEN_LONG,
        qty=Decimal("1"), price=Decimal("100"), leverage=1,
    ))
    _, kwargs = client.market_order.call_args
    assert kwargs["symbol"] == "ETH"


# ===================================================================
# Leverage must actually apply or the trade is aborted (the naked-10x incident):
# a failed update_leverage was previously only WARNED, so the order opened at the
# exchange's default leverage. And HIP-3 equity perps are ISOLATED-ONLY.
# ===================================================================

def test_leverage_update_failure_aborts_trade(monkeypatch):
    """A non-'ok' leverage response must ABORT the trade (raise) and place NO order,
    never opening at the exchange's default leverage."""
    svc, client = _service(monkeypatch)
    client.update_leverage.return_value = {
        "status": "err", "response": "Cross margin is not allowed for this asset."
    }
    with pytest.raises(HyperliquidValidationError):
        svc.place_order(OrderRequest(
            symbol="xyz:SMSN", side=Side.BUY, signal=SignalType.OPEN_LONG,
            qty=Decimal("1"), price=Decimal("100"), leverage=2,
        ))
    client.market_order.assert_not_called()  # never reached order submission


def test_dex_symbol_uses_isolated_margin(monkeypatch):
    """HIP-3 dex/equity perps (':' in symbol) are isolated-only, so update_leverage
    must be called with is_cross=False for them."""
    svc, client = _service(monkeypatch)
    svc.place_order(OrderRequest(
        symbol="xyz:SMSN", side=Side.BUY, signal=SignalType.OPEN_LONG,
        qty=Decimal("1"), price=Decimal("100"), leverage=2,
    ))
    _, kwargs = client.update_leverage.call_args
    assert kwargs.get("is_cross") is False


def test_main_perp_uses_cross_margin(monkeypatch):
    """Plain (non-dex) perps keep cross margin (is_cross=True) — unchanged behavior."""
    svc, client = _service(monkeypatch)
    svc.place_order(OrderRequest(
        symbol="SOL", side=Side.BUY, signal=SignalType.OPEN_LONG,
        qty=Decimal("1"), price=Decimal("100"), leverage=2,
    ))
    _, kwargs = client.update_leverage.call_args
    assert kwargs.get("is_cross") is True


def test_close_order_skips_leverage_update(monkeypatch):
    """A CLOSE/reduce order must NOT touch leverage: Hyperliquid can reject a margin/
    leverage change while a position is OPEN, and the fatal-abort would then block the
    EXIT (the worst failure for live money). Leverage is set on OPENS only; a leverage
    error must never prevent flattening."""
    svc, client = _service(monkeypatch)
    client.update_leverage.return_value = {"status": "err", "response": "blocked on open position"}
    svc.place_order(OrderRequest(
        symbol="xyz:SMSN", side=Side.SELL, signal=SignalType.CLOSE_LONG,
        qty=Decimal("1"), price=Decimal("100"), leverage=2,
    ))
    client.update_leverage.assert_not_called()  # never touched on a close
    client.close_position.assert_called_once()  # the exit still goes through
