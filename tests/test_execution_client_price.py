"""Tests for _normalize_price — it must emit prices Hyperliquid accepts.

Hyperliquid rejects a perp price with more than 5 significant figures OR more than
(6 - szDecimals) decimal places ("Order has invalid price."). A prior bug used
szDecimals as a price tick (10^-szDecimals) — szDecimals is a SIZE precision, so
that produced over-precise prices the exchange rejected (e.g. an ETH-like 1652.333
became 1652.3330 -> rejected).
"""

from __future__ import annotations

import pathlib
import sys
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = str(pathlib.Path(__file__).resolve().parents[1])
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from hypertrade.routes.hyperliquid_execution_client import HyperliquidExecutionClient, PositionSide


def _client(monkeypatch, sz_decimals: int) -> HyperliquidExecutionClient:
    """A real execution client with the SDK Exchange + data client stubbed, and
    get_meta returning the given szDecimals. _normalize_price stays REAL (unlike the
    cloid suite, which mocks it) so we exercise the actual rounding."""
    monkeypatch.setenv("HYPERTRADE_ENVIRONMENT", "test")
    monkeypatch.setenv("HYPERTRADE_MASTER_ADDR", "0xMASTER")
    monkeypatch.setenv("HYPERTRADE_API_WALLET_PRIV", "k")
    monkeypatch.setenv("HYPERTRADE_WEBHOOK_SECRET", "secret")
    from hypertrade.config import get_settings
    get_settings.cache_clear()
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
    client.data.get_meta = MagicMock(return_value={"szDecimals": sz_decimals})
    return client


def _is_valid_hl_perp_price(px: float, sz_decimals: int) -> bool:
    """Mirror Hyperliquid's perp price rule independently of the implementation:
    integers are always allowed; otherwise <=5 significant figures AND
    <=(6 - szDecimals) decimal places."""
    d = Decimal(str(px))
    if d == d.to_integral_value():
        return True
    decimals = -d.as_tuple().exponent
    sig_figs = len(d.normalize().as_tuple().digits)
    return decimals <= (6 - sz_decimals) and sig_figs <= 5


@pytest.mark.parametrize(
    "sz_decimals,price",
    [
        (4, 1652.333),    # ETH-like — the exact case that was rejected
        (4, 1652.0),
        (5, 60123.45),    # BTC-like (5 sig figs -> integer grid)
        (3, 21.98765),    # SOL-like
        (2, 3.14159),
        (0, 0.00012345),  # low-priced coin
    ],
)
def test_normalize_price_is_valid_for_hyperliquid(monkeypatch, sz_decimals, price):
    client = _client(monkeypatch, sz_decimals)
    for is_buy in (True, False):
        px = client._normalize_price("SOL", price, is_buy=is_buy)
        assert _is_valid_hl_perp_price(px, sz_decimals), (
            f"{px} is not a valid Hyperliquid price for szDecimals={sz_decimals}"
        )


def test_normalize_price_eth_regression(monkeypatch):
    """The exact value Hyperliquid rejected as 'Order has invalid price.': an
    ETH-like price (szDecimals 4 -> at most 2 decimals, <=5 sig figs) lands on the
    valid grid as 1652.3 instead of the old 1652.3330."""
    client = _client(monkeypatch, 4)
    assert client._normalize_price("ETH", 1652.333, is_buy=True) == 1652.3


def test_normalize_price_rejects_nonpositive(monkeypatch):
    client = _client(monkeypatch, 4)
    with pytest.raises(ValueError):
        client._normalize_price("ETH", 0.0, is_buy=True)


def test_market_order_prices_off_mid_with_slippage(monkeypatch):
    """A true MARKET order: an aggressive IOC priced off the MID by the slippage cap —
    BUY above mid, SELL below mid — so it always crosses regardless of book shape (no
    dependence on impact prices). Submitted as an IOC limit at mid*(1 ± slippage)."""
    client = _client(monkeypatch, 3)
    client.data.get_mid = MagicMock(return_value=1000.0)

    client.market_order(symbol="xyz:KR200", side=PositionSide.SHORT, size=1.0, premium_bps=500)
    sell_px = client.exchange.order.call_args.args[3]   # exchange.order(coin, is_buy, sz, px, ...)
    assert sell_px == 950.0, f"SELL must price 5% BELOW mid (crosses down to fill), got {sell_px}"

    client.market_order(symbol="xyz:KR200", side=PositionSide.LONG, size=1.0, premium_bps=500)
    buy_px = client.exchange.order.call_args.args[3]
    assert buy_px == 1050.0, f"BUY must price 5% ABOVE mid (crosses up to fill), got {buy_px}"


def test_market_order_logs_submitted_pricing(monkeypatch, caplog):
    """Before submitting a MARKET IOC, the client logs the mid and the exact
    aggressive/normalized price it is about to send, tagged with the cloid — the
    primary diagnostic for a 'bad price / could not match' reject."""
    import logging as _logging
    client = _client(monkeypatch, 3)
    client.data.get_mid = MagicMock(return_value=1000.0)

    with caplog.at_level(_logging.INFO, logger="uvicorn.error"):
        client.market_order(
            symbol="SOL", side=PositionSide.LONG, size=2.0,
            premium_bps=500, cloid="0x" + "a" * 32,
        )

    msgs = [r.getMessage() for r in caplog.records]
    assert any(
        "mid=1000" in m and "norm_px=1050" in m and ("0x" + "a" * 32) in m
        for m in msgs
    ), msgs


def test_limit_order_logs_submitted_pricing(monkeypatch, caplog):
    """limit_order logs the normalized price it is about to submit, tagged with cloid."""
    import logging as _logging
    client = _client(monkeypatch, 3)
    client.exchange.order.return_value = {
        "response": {"data": {"statuses": [{"resting": {"oid": 1}}]}}
    }
    with caplog.at_level(_logging.INFO, logger="uvicorn.error"):
        client.limit_order(
            symbol="SOL", side=PositionSide.LONG, size=2.0, price=21.987,
            tif="Gtc", cloid="0x" + "c" * 32,
        )
    msgs = [r.getMessage() for r in caplog.records]
    assert any(
        "Submitting LIMIT" in m and "norm_price=" in m and ("0x" + "c" * 32) in m
        for m in msgs
    ), msgs
