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

from hypertrade.routes.hyperliquid_service import HyperliquidService, OrderRequest
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
