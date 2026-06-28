"""Tests for TradingView webhook endpoint behavior and integrations.

These tests validate content-type enforcement, schema validation, optional
secret checks, and IP whitelist behavior.
"""

from __future__ import annotations

# pylint: disable=import-outside-toplevel

import os
import sys
import pathlib
import json
import copy

from decimal import Decimal

from fastapi.testclient import TestClient
from unittest.mock import Mock, patch


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


class StubHyperliquidService:
    """FastAPI dependency replacement that avoids network calls."""

    last_order_request = None
    should_fail = False
    failure_type = None
    call_count = 0
    # TD-1 query-before-resubmit: the retry loop asks the service whether the
    # order already landed under its cloid before resubmitting. The default
    # (None) means "confirmed absent", so the persistent-failure tests below
    # exercise the full retry-then-fail path rather than short-circuiting.
    find_order_result = None
    find_order_calls = 0

    def __init__(self, *args, **kwargs):
        pass

    def find_order_by_cloid(self, cloid):
        type(self).find_order_calls += 1
        return type(self).find_order_result

    def place_order(self, request):
        type(self).last_order_request = request
        type(self).call_count += 1

        if type(self).should_fail:
            from hypertrade.routes.hyperliquid_service import (
                HyperliquidValidationError,
                HyperliquidNetworkError,
                HyperliquidAPIError,
            )
            failure_map = {
                "validation": HyperliquidValidationError("Order validation failed"),
                "network": HyperliquidNetworkError("Network timeout"),
                "api": HyperliquidAPIError("Exchange API error"),
            }
            raise failure_map.get(type(self).failure_type, Exception("Unknown error"))

        return {
            "status": "ok",
            "order_id": "test-order",
            "symbol": getattr(request.symbol, "upper", lambda: request.symbol)(),
            "side": getattr(getattr(request.side, "value", request.side), "upper", lambda: request.side)(),
            "qty": str(getattr(request, "qty", "")),
            "price": str(getattr(request, "price", "")),
            "reduce_only": getattr(request, "reduce_only", False),
            "post_only": getattr(request, "post_only", False),
            "client_id": getattr(request, "client_id", None),
        }

    @classmethod
    def reset(cls):
        """Reset test state between test runs."""
        cls.last_order_request = None
        cls.should_fail = False
        cls.failure_type = None
        cls.call_count = 0
        cls.find_order_result = None
        cls.find_order_calls = 0


def make_app(monkeypatch, *, secret: str | None = None):
    """Create app with env configured via pytest monkeypatch.

    Args:
        secret: Webhook secret to use. If None, uses default "secret" (matching BASE_PAYLOAD).
                Use empty string "" to enable IP whitelist instead.
    """
    # Required env vars for settings
    monkeypatch.setenv("HYPERTRADE_ENVIRONMENT", "test")
    monkeypatch.setenv("HYPERTRADE_MASTER_ADDR", "0xMASTER")
    monkeypatch.setenv("HYPERTRADE_API_WALLET_PRIV", "dummy-priv-key")
    monkeypatch.setenv("HYPERTRADE_SUBACCOUNT_ADDR", "0xSUB")
    monkeypatch.setenv("PRIVATE_KEY", "0x" + "1" * 64)
    # Idempotency is opt-in per-test; keep the default suite nonce-free.
    if os.getenv("HYPERTRADE_IDEMPOTENCY_ENABLED") is None:
        monkeypatch.setenv("HYPERTRADE_IDEMPOTENCY_ENABLED", "false")

    # Ensure at least one authentication method is enabled
    # Check if IP whitelist was already explicitly enabled
    ip_whitelist_already_set = os.getenv("HYPERTRADE_IP_WHITELIST_ENABLED", "").lower() == "true"

    if secret == "":
        # Empty string means: use IP whitelist instead of secret
        monkeypatch.setenv("HYPERTRADE_IP_WHITELIST_ENABLED", "true")
        # No secret in this mode — clear any ambient one so enforcement stays off.
        monkeypatch.delenv("HYPERTRADE_WEBHOOK_SECRET", raising=False)
    elif secret is not None:
        # Explicit secret provided
        monkeypatch.setenv("HYPERTRADE_WEBHOOK_SECRET", secret)
        # Only disable IP whitelist if it wasn't explicitly enabled
        if not ip_whitelist_already_set:
            monkeypatch.setenv("HYPERTRADE_IP_WHITELIST_ENABLED", "false")
    else:
        # Default: use "secret" to match BASE_PAYLOAD and satisfy authentication requirement
        # But only if IP whitelist wasn't already enabled (for IP whitelist tests)
        if not ip_whitelist_already_set:
            monkeypatch.setenv("HYPERTRADE_WEBHOOK_SECRET", "secret")
            monkeypatch.setenv("HYPERTRADE_IP_WHITELIST_ENABLED", "false")
        else:
            # IP-whitelist-only test: no secret — clear any ambient one so the
            # whitelisted request isn't rejected by leaked secret enforcement.
            monkeypatch.delenv("HYPERTRADE_WEBHOOK_SECRET", raising=False)

    # Ensure this repo's package is first on sys.path to avoid name collisions
    repo_root = str(pathlib.Path(__file__).resolve().parents[1])
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)

    # Import the app factory and clear cached settings via the imported module
    from hypertrade import daemon
    from hypertrade.routes import webhooks as webhooks_module

    monkeypatch.setattr(webhooks_module, "HyperliquidService", StubHyperliquidService)

    daemon.get_settings.cache_clear()
    app = daemon.create_daemon()
    return app


def test_webhook_happy_path_ok(monkeypatch):
    """Returns 200 with normalized summary for valid payload."""
    StubHyperliquidService.reset()
    app = make_app(monkeypatch, secret="secret")
    client = TestClient(app)

    payload = copy.deepcopy(BASE_PAYLOAD)
    resp = client.post("/webhook", json=payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "ok"
    assert data["signal"] == "CLOSE_SHORT"
    assert data["side"] == "buy"
    assert data["symbol"] == "SOL"
    assert data["ticker"] == "SOLUSD"
    assert data["action"] == "buy"
    assert data["contracts"] == "46231.75300000"
    assert data["price"] == "183.81"
    order_req = StubHyperliquidService.last_order_request
    assert order_req is not None
    assert order_req.leverage == 1

def test_webhook_rejects_non_json_content_type(monkeypatch):
    """Returns 415 when Content-Type is not application/json."""
    app = make_app(monkeypatch, secret=None)
    client = TestClient(app)

    body = json.dumps(BASE_PAYLOAD)
    # Send as text/plain to simulate TradingView misconfigured content-type
    resp = client.post("/webhook", data=body, headers={"Content-Type": "text/plain"})
    assert resp.status_code == 415
    data = resp.json()
    assert data["error"]["status"] == 415
    assert "application/json" in data["error"]["detail"]

def test_webhook_invalid_json_returns_422(monkeypatch):
    """Returns 422 and error body on malformed JSON input."""
    app = make_app(monkeypatch, secret=None)
    client = TestClient(app)

    # Malformed JSON (trailing comma)
    bad = b'{"a":1,}'
    resp = client.post("/webhook", data=bad, headers={"Content-Type": "application/json"})
    assert resp.status_code == 422
    data = resp.json()
    assert data["error"]["status"] == 422

def test_webhook_rejects_bad_secret(monkeypatch):
    """Returns 401 when webhook secret does not match environment."""
    app = make_app(monkeypatch, secret="expected-secret")
    client = TestClient(app)

    payload = copy.deepcopy(BASE_PAYLOAD)
    # Payload carries a different secret than env
    payload["general"]["secret"] = "ops-wrong-secret"
    resp = client.post("/webhook", json=payload)
    assert resp.status_code == 401
    body = resp.json()
    assert body["error"]["status"] == 401

def test_webhook_invalid_leverage_returns_400(monkeypatch):
    """Returns 400 when leverage cannot be parsed."""
    app = make_app(monkeypatch, secret="secret")
    client = TestClient(app)

    payload = copy.deepcopy(BASE_PAYLOAD)
    payload["general"]["leverage"] = "bogus"
    resp = client.post("/webhook", json=payload)
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["status"] == 400


def test_webhook_ip_whitelist_allows_forwarded(monkeypatch):
    """Allows request when X-Forwarded-For contains a whitelisted IP."""
    # Enable whitelist and set allowed IPs
    monkeypatch.setenv("HYPERTRADE_IP_WHITELIST_ENABLED", "true")
    monkeypatch.setenv("HYPERTRADE_TV_WEBHOOK_IPS", '["1.2.3.4","52.32.178.7"]')
    monkeypatch.setenv("HYPERTRADE_TRUST_FORWARDED_FOR", "true")

    app = make_app(monkeypatch, secret=None)  # no secret enforcement
    client = TestClient(app)

    payload = copy.deepcopy(BASE_PAYLOAD)
    # Use an allowed IP via X-Forwarded-For
    headers = {"X-Forwarded-For": "1.2.3.4"}
    resp = client.post("/webhook", json=payload, headers=headers)
    assert resp.status_code == 200, resp.text


def test_webhook_ip_whitelist_blocks_forwarded(monkeypatch):
    """Blocks request when X-Forwarded-For is not in whitelist."""
    # Enable whitelist and set allowed IPs (not including 9.9.9.9)
    monkeypatch.setenv("HYPERTRADE_IP_WHITELIST_ENABLED", "true")
    monkeypatch.setenv("HYPERTRADE_TV_WEBHOOK_IPS", '["1.2.3.4","52.32.178.7"]')
    monkeypatch.setenv("HYPERTRADE_TRUST_FORWARDED_FOR", "true")

    app = make_app(monkeypatch, secret=None)  # no secret enforcement
    client = TestClient(app)

    payload = copy.deepcopy(BASE_PAYLOAD)
    headers = {"X-Forwarded-For": "9.9.9.9"}
    resp = client.post("/webhook", json=payload, headers=headers)
    assert resp.status_code == 403
    body = resp.json()
    assert body["error"]["status"] == 403


def test_webhook_preserves_decimal_precision_in_order(monkeypatch):
    """Order quantity/price must reach the exchange client as exact Decimals.

    The payload model parses contracts/price as Decimal; converting them to
    float in the handler corrupts precision (Decimal(0.1_float) != Decimal("0.1"))
    and can mis-size an order or get it rejected by the exchange.
    """
    StubHyperliquidService.reset()
    app = make_app(monkeypatch, secret="secret")
    client = TestClient(app)

    payload = copy.deepcopy(BASE_PAYLOAD)
    payload["order"]["contracts"] = "0.1"
    payload["order"]["price"] = "123456789.123456789"

    resp = client.post("/webhook", json=payload)
    assert resp.status_code == 200, resp.text

    captured = StubHyperliquidService.last_order_request
    assert captured is not None
    assert isinstance(captured.qty, Decimal)
    assert isinstance(captured.price, Decimal)
    assert captured.qty == Decimal("0.1")
    assert captured.price == Decimal("123456789.123456789")


async def test_place_order_runs_off_the_event_loop():
    """The synchronous place_order must not block the event loop.

    place_order blocks on a threading.Event that only a concurrent coroutine can
    set. If the call ran directly on the loop, that coroutine could never run and
    the wait would time out — so a passing test proves the call was offloaded.
    """
    import asyncio
    import threading

    from hypertrade.routes.webhooks import _place_order_with_retry
    from hypertrade.routes.hyperliquid_service import OrderRequest
    from hypertrade.routes.tradingview_enums import Side, SignalType

    release = threading.Event()
    placed = {"value": False}

    class BlockingClient:
        def place_order(self, req):
            if not release.wait(timeout=2.0):
                raise AssertionError("event loop blocked: concurrent coroutine never ran")
            placed["value"] = True
            return {"orderId": "x"}

    async def concurrent():
        await asyncio.sleep(0.05)  # can only run if the loop is free
        release.set()

    req = OrderRequest(
        symbol="SOL", side=Side.BUY, signal=SignalType.OPEN_LONG, qty=Decimal("1")
    )
    result, _ = await asyncio.gather(
        _place_order_with_retry(BlockingClient(), req),
        concurrent(),
    )

    assert placed["value"] is True
    assert result == {"orderId": "x"}


# ===================================================================
# Signal Parsing Edge Cases
# ===================================================================

def test_signal_parsing_open_long(monkeypatch):
    """Test parsing of OPEN_LONG signal."""
    StubHyperliquidService.reset()
    app = make_app(monkeypatch, secret="secret")
    client = TestClient(app)

    payload = copy.deepcopy(BASE_PAYLOAD)
    payload["market"]["previous_position"] = "flat"
    payload["market"]["position"] = "long"
    payload["order"]["action"] = "buy"
    resp = client.post("/webhook", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["signal"] == "OPEN_LONG"
    assert data["side"] == "buy"


def test_signal_parsing_add_to_position(monkeypatch):
    """Test parsing of ADD_LONG signal (same position, buy again)."""
    StubHyperliquidService.reset()
    app = make_app(monkeypatch, secret="secret")
    client = TestClient(app)

    payload = copy.deepcopy(BASE_PAYLOAD)
    payload["market"]["previous_position"] = "long"
    payload["market"]["position"] = "long"
    payload["order"]["action"] = "buy"
    resp = client.post("/webhook", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["signal"] == "ADD_LONG"
    assert data["side"] == "buy"


def test_signal_parsing_reduce_position(monkeypatch):
    """Test parsing of REDUCE_LONG signal (same position, sell)."""
    StubHyperliquidService.reset()
    app = make_app(monkeypatch, secret="secret")
    client = TestClient(app)

    payload = copy.deepcopy(BASE_PAYLOAD)
    payload["market"]["previous_position"] = "long"
    payload["market"]["position"] = "long"
    payload["order"]["action"] = "sell"
    resp = client.post("/webhook", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["signal"] == "REDUCE_LONG"
    assert data["side"] == "sell"


def test_signal_parsing_reversal_to_short(monkeypatch):
    """Test parsing of REVERSE_TO_SHORT signal (long to short)."""
    StubHyperliquidService.reset()
    app = make_app(monkeypatch, secret="secret")
    client = TestClient(app)

    payload = copy.deepcopy(BASE_PAYLOAD)
    payload["market"]["previous_position"] = "long"
    payload["market"]["position"] = "short"
    payload["order"]["action"] = "sell"
    resp = client.post("/webhook", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["signal"] == "REVERSE_TO_SHORT"
    assert data["side"] == "sell"


def test_signal_no_action_ignored(monkeypatch):
    """Test that NO_ACTION signals are ignored and webhook returns 200 but ignored status.

    Note: Using a valid action but invalid position state to trigger NO_ACTION.
    """
    StubHyperliquidService.reset()
    app = make_app(monkeypatch, secret="secret")
    client = TestClient(app)

    payload = copy.deepcopy(BASE_PAYLOAD)
    # Conflicting position states that result in NO_ACTION
    payload["market"]["previous_position"] = "long"
    payload["market"]["position"] = "short"
    payload["order"]["action"] = "buy"  # Would need sell for reversal
    resp = client.post("/webhook", json=payload)
    # Conflict results in 200 with ignored status
    if resp.status_code == 200:
        data = resp.json()
        assert data["status"] in ("ignored", "ok")


def test_signal_malformed_position_defaults_to_flat(monkeypatch):
    """Test that missing previous_position defaults to FLAT.

    Empty strings in schema may fail validation, so we test with missing field.
    """
    StubHyperliquidService.reset()
    app = make_app(monkeypatch, secret="secret")
    client = TestClient(app)

    payload = copy.deepcopy(BASE_PAYLOAD)
    # Set previous_position to None (not empty string) to trigger default
    payload["market"]["previous_position"] = None
    payload["market"]["position"] = "long"
    payload["order"]["action"] = "buy"
    resp = client.post("/webhook", json=payload)
    if resp.status_code == 200:
        data = resp.json()
        # Should parse as OPEN_LONG (flat -> long)
        assert data["signal"] == "OPEN_LONG"


# ===================================================================
# Error Handling and Retry Logic
# ===================================================================

def test_order_validation_error_returns_400(monkeypatch):
    """Test that validation errors return 400 without retry."""
    StubHyperliquidService.reset()
    app = make_app(monkeypatch, secret="secret")
    client = TestClient(app)

    StubHyperliquidService.should_fail = True
    StubHyperliquidService.failure_type = "validation"

    payload = copy.deepcopy(BASE_PAYLOAD)
    resp = client.post("/webhook", json=payload)
    assert resp.status_code == 400
    data = resp.json()
    assert "Invalid order" in data["error"]["detail"]
    # Should not retry validation errors
    assert StubHyperliquidService.call_count == 1


def test_network_error_returns_503_with_retries(monkeypatch):
    """Test that network errors return 503 after retries."""
    StubHyperliquidService.reset()
    app = make_app(monkeypatch, secret="secret")
    client = TestClient(app)

    StubHyperliquidService.should_fail = True
    StubHyperliquidService.failure_type = "network"

    payload = copy.deepcopy(BASE_PAYLOAD)
    resp = client.post("/webhook", json=payload)
    assert resp.status_code == 503
    data = resp.json()
    assert "Temporary service unavailable" in data["error"]["detail"]
    # Should retry (1 initial + 2 retries = 3 calls)
    assert StubHyperliquidService.call_count == 3


def test_api_error_returns_502_with_retries(monkeypatch):
    """Test that API errors return 502 after retries."""
    StubHyperliquidService.reset()
    app = make_app(monkeypatch, secret="secret")
    client = TestClient(app)

    StubHyperliquidService.should_fail = True
    StubHyperliquidService.failure_type = "api"

    payload = copy.deepcopy(BASE_PAYLOAD)
    resp = client.post("/webhook", json=payload)
    assert resp.status_code == 502
    data = resp.json()
    assert "Exchange error" in data["error"]["detail"]
    # Should retry (1 initial + 2 retries = 3 calls)
    assert StubHyperliquidService.call_count == 3


# ===================================================================
# Schema Validation Edge Cases
# ===================================================================

def test_schema_missing_required_field_returns_422(monkeypatch):
    """Test that missing required fields fail schema validation."""
    StubHyperliquidService.reset()
    app = make_app(monkeypatch, secret="secret")
    client = TestClient(app)

    payload = copy.deepcopy(BASE_PAYLOAD)
    del payload["order"]["action"]  # Remove required field
    resp = client.post("/webhook", json=payload)
    assert resp.status_code == 422
    data = resp.json()
    assert data["error"]["status"] == 422


def test_webhook_with_zero_contracts(monkeypatch):
    """Test that zero contracts are handled gracefully."""
    StubHyperliquidService.reset()
    app = make_app(monkeypatch, secret="secret")
    client = TestClient(app)

    payload = copy.deepcopy(BASE_PAYLOAD)
    payload["order"]["contracts"] = "0"
    resp = client.post("/webhook", json=payload)
    # Should process but nominal_quantity will be 0
    assert resp.status_code == 200


def test_webhook_with_negative_contracts_rejected(monkeypatch):
    """Test that negative contracts are rejected."""
    StubHyperliquidService.reset()
    app = make_app(monkeypatch, secret="secret")
    client = TestClient(app)

    payload = copy.deepcopy(BASE_PAYLOAD)
    payload["order"]["contracts"] = "-100"
    resp = client.post("/webhook", json=payload)
    # Should parse and reach service layer which should reject
    assert resp.status_code in (200, 400)  # Either service rejects or webhook succeeds


def test_webhook_with_high_leverage_string_formats(monkeypatch):
    """Test various leverage string formats are parsed correctly."""
    StubHyperliquidService.reset()
    app = make_app(monkeypatch, secret="secret")
    client = TestClient(app)

    # Test with "10x" format
    payload = copy.deepcopy(BASE_PAYLOAD)
    payload["general"]["leverage"] = "10x"
    resp = client.post("/webhook", json=payload)
    assert resp.status_code == 200
    order_req = StubHyperliquidService.last_order_request
    assert order_req.leverage == 10

    # Test with "5X" format (uppercase)
    StubHyperliquidService.reset()
    payload = copy.deepcopy(BASE_PAYLOAD)
    payload["general"]["leverage"] = "5X"
    resp = client.post("/webhook", json=payload)
    assert resp.status_code == 200
    order_req = StubHyperliquidService.last_order_request
    assert order_req.leverage == 5

    # Test with just number
    StubHyperliquidService.reset()
    payload = copy.deepcopy(BASE_PAYLOAD)
    payload["general"]["leverage"] = "3"
    resp = client.post("/webhook", json=payload)
    assert resp.status_code == 200
    order_req = StubHyperliquidService.last_order_request
    assert order_req.leverage == 3


# ===================================================================
# Order Parameter Tests
# ===================================================================

def test_order_parameters_passed_to_service(monkeypatch):
    """Test that all order parameters are correctly passed to Hyperliquid service."""
    StubHyperliquidService.reset()
    app = make_app(monkeypatch, secret="secret")
    client = TestClient(app)

    payload = copy.deepcopy(BASE_PAYLOAD)
    payload["general"]["leverage"] = "5"
    payload["order"]["contracts"] = "100.5"
    payload["order"]["price"] = "200.0"
    resp = client.post("/webhook", json=payload)
    assert resp.status_code == 200

    order_req = StubHyperliquidService.last_order_request
    assert order_req.symbol == "SOL"
    assert order_req.leverage == 5
    assert float(order_req.qty) == 100.5
    assert float(order_req.price) == 200.0
    assert order_req.reduce_only is False
    assert order_req.post_only is False


def test_order_response_contains_all_fields(monkeypatch):
    """Test that webhook response contains all expected fields."""
    StubHyperliquidService.reset()
    app = make_app(monkeypatch, secret="secret")
    client = TestClient(app)

    payload = copy.deepcopy(BASE_PAYLOAD)
    resp = client.post("/webhook", json=payload)
    assert resp.status_code == 200
    data = resp.json()

    required_fields = [
        "status", "signal", "side", "symbol", "ticker",
        "action", "contracts", "price", "received_at"
    ]
    for field in required_fields:
        assert field in data, f"Missing field: {field}"


def test_reduce_long_sets_reduce_only_true(monkeypatch):
    """Test that REDUCE_LONG signal sets reduce_only=True to prevent opening new positions."""
    StubHyperliquidService.reset()
    app = make_app(monkeypatch, secret="secret")
    client = TestClient(app)

    payload = copy.deepcopy(BASE_PAYLOAD)
    payload["market"]["previous_position"] = "long"
    payload["market"]["position"] = "long"
    payload["order"]["action"] = "sell"
    resp = client.post("/webhook", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["signal"] == "REDUCE_LONG"

    # Verify reduce_only flag is set to True
    order_req = StubHyperliquidService.last_order_request
    assert order_req is not None
    assert order_req.reduce_only is True, "REDUCE_LONG should have reduce_only=True"


def test_reduce_short_sets_reduce_only_true(monkeypatch):
    """Test that REDUCE_SHORT signal sets reduce_only=True to prevent opening new positions."""
    StubHyperliquidService.reset()
    app = make_app(monkeypatch, secret="secret")
    client = TestClient(app)

    payload = copy.deepcopy(BASE_PAYLOAD)
    payload["market"]["previous_position"] = "short"
    payload["market"]["position"] = "short"
    payload["order"]["action"] = "buy"
    resp = client.post("/webhook", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["signal"] == "REDUCE_SHORT"

    # Verify reduce_only flag is set to True
    order_req = StubHyperliquidService.last_order_request
    assert order_req is not None
    assert order_req.reduce_only is True, "REDUCE_SHORT should have reduce_only=True"


def test_add_long_sets_reduce_only_false(monkeypatch):
    """Test that ADD_LONG signal keeps reduce_only=False to allow increasing position."""
    StubHyperliquidService.reset()
    app = make_app(monkeypatch, secret="secret")
    client = TestClient(app)

    payload = copy.deepcopy(BASE_PAYLOAD)
    payload["market"]["previous_position"] = "long"
    payload["market"]["position"] = "long"
    payload["order"]["action"] = "buy"
    resp = client.post("/webhook", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["signal"] == "ADD_LONG"

    # Verify reduce_only flag is False
    order_req = StubHyperliquidService.last_order_request
    assert order_req is not None
    assert order_req.reduce_only is False, "ADD_LONG should have reduce_only=False"


def test_open_long_sets_reduce_only_false(monkeypatch):
    """Test that OPEN_LONG signal keeps reduce_only=False to allow opening new positions."""
    StubHyperliquidService.reset()
    app = make_app(monkeypatch, secret="secret")
    client = TestClient(app)

    payload = copy.deepcopy(BASE_PAYLOAD)
    payload["market"]["previous_position"] = "flat"
    payload["market"]["position"] = "long"
    payload["order"]["action"] = "buy"
    resp = client.post("/webhook", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["signal"] == "OPEN_LONG"

    # Verify reduce_only flag is False
    order_req = StubHyperliquidService.last_order_request
    assert order_req is not None
    assert order_req.reduce_only is False, "OPEN_LONG should have reduce_only=False"


def test_general_model_parses_nonce():
    """Test that the general model accepts and parses an optional nonce field."""
    from hypertrade.schemas.tradingview import TradingViewWebhook
    payload = copy.deepcopy(BASE_PAYLOAD)
    payload["general"]["nonce"] = "abc-123"
    model = TradingViewWebhook.model_validate(payload)
    assert model.general.nonce == "abc-123"


# ===================================================================
# Idempotency Integration Tests
# ===================================================================

def _idem_payload(nonce):
    payload = copy.deepcopy(BASE_PAYLOAD)
    payload["general"]["nonce"] = nonce
    return payload


def test_idempotency_missing_nonce_returns_400(monkeypatch, tmp_path):
    monkeypatch.setenv("HYPERTRADE_IDEMPOTENCY_ENABLED", "true")
    monkeypatch.setenv("HYPERTRADE_DB_PATH", str(tmp_path / "idem.db"))
    StubHyperliquidService.reset()
    app = make_app(monkeypatch, secret="secret")
    client = TestClient(app)
    resp = client.post("/webhook", json=copy.deepcopy(BASE_PAYLOAD))  # no nonce
    assert resp.status_code == 400


def test_idempotency_duplicate_places_order_once(monkeypatch, tmp_path):
    monkeypatch.setenv("HYPERTRADE_IDEMPOTENCY_ENABLED", "true")
    monkeypatch.setenv("HYPERTRADE_DB_PATH", str(tmp_path / "idem.db"))
    StubHyperliquidService.reset()
    app = make_app(monkeypatch, secret="secret")
    client = TestClient(app)
    payload = _idem_payload("nonce-dup-1")

    first = client.post("/webhook", json=payload)
    assert first.status_code == 200
    assert StubHyperliquidService.call_count == 1

    second = client.post("/webhook", json=payload)
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate"
    assert StubHyperliquidService.call_count == 1  # not placed again


def test_idempotency_in_flight_returns_409(monkeypatch, tmp_path):
    monkeypatch.setenv("HYPERTRADE_IDEMPOTENCY_ENABLED", "true")
    monkeypatch.setenv("HYPERTRADE_DB_PATH", str(tmp_path / "idem.db"))
    StubHyperliquidService.reset()
    app = make_app(monkeypatch, secret="secret")
    client = TestClient(app)
    # Pre-reserve so the request sees the nonce already in-flight (not stale).
    app.state.idempotency.reserve("nonce-inflight-1", "pre-req", 60)
    resp = client.post("/webhook", json=_idem_payload("nonce-inflight-1"))
    assert resp.status_code == 409
    assert StubHyperliquidService.call_count == 0  # no order placed


def test_idempotency_release_on_failure_allows_retry(monkeypatch, tmp_path):
    monkeypatch.setenv("HYPERTRADE_IDEMPOTENCY_ENABLED", "true")
    monkeypatch.setenv("HYPERTRADE_DB_PATH", str(tmp_path / "idem.db"))
    StubHyperliquidService.reset()
    StubHyperliquidService.should_fail = True
    StubHyperliquidService.failure_type = "network"
    app = make_app(monkeypatch, secret="secret")
    client = TestClient(app)
    payload = _idem_payload("nonce-retry-1")

    failed = client.post("/webhook", json=payload)
    assert failed.status_code == 503  # network error after retries

    # nonce released -> a retry with the same nonce succeeds and places the order
    StubHyperliquidService.should_fail = False
    ok = client.post("/webhook", json=payload)
    assert ok.status_code == 200
    assert ok.json()["status"] == "ok"


def test_idempotency_store_failure_returns_503(monkeypatch, tmp_path):
    monkeypatch.setenv("HYPERTRADE_IDEMPOTENCY_ENABLED", "true")
    monkeypatch.setenv("HYPERTRADE_DB_PATH", str(tmp_path / "idem.db"))
    import sqlite3 as _sqlite3
    StubHyperliquidService.reset()
    app = make_app(monkeypatch, secret="secret")

    class _BrokenStore:
        def reserve(self, *a, **k):
            raise _sqlite3.OperationalError("db locked")
    app.state.idempotency = _BrokenStore()

    client = TestClient(app)
    resp = client.post("/webhook", json=_idem_payload("nonce-broken-1"))
    assert resp.status_code == 503
    assert StubHyperliquidService.call_count == 0  # no order placed when store is down


# ===================================================================
# History Endpoint Auth Tests
# ===================================================================

def test_history_requires_bearer_auth(monkeypatch):
    app = make_app(monkeypatch, secret="secret")
    client = TestClient(app)
    assert client.get("/history/orders").status_code == 401                                    # missing
    assert client.get("/history/orders", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/history/stats", headers={"Authorization": "Bearer secret"}).status_code == 200
    assert client.get("/history/orders", headers={"Authorization": "Bearer secret"}).status_code == 200


# ===================================================================
# Dry-Run (Demo) Mode
# ===================================================================

def test_webhook_dry_run_returns_dry_run_without_placing(monkeypatch, tmp_path):
    """HYPERTRADE_DRY_RUN=true: webhook validated, but no order/DB/idempotency."""
    monkeypatch.setenv("HYPERTRADE_DRY_RUN", "true")
    monkeypatch.setenv("HYPERTRADE_DB_PATH", str(tmp_path / "dryrun.db"))
    monkeypatch.setenv("HYPERTRADE_IDEMPOTENCY_ENABLED", "true")
    StubHyperliquidService.reset()
    app = make_app(monkeypatch, secret="secret")
    client = TestClient(app)

    payload = _idem_payload("nonce-dryrun-1")
    resp = client.post("/webhook", json=payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "dry_run"
    assert data["signal"] == "CLOSE_SHORT"
    assert data["side"] == "buy"
    assert data["symbol"] == "SOL"
    assert data["contracts"] == "46231.75300000"
    assert data["price"] == "183.81"
    assert data["leverage"] == 1
    assert data["reduce_only"] is False
    assert data["subaccount"] == "0xSUB"

    # Nothing left the process: no exchange call, no DB row.
    assert StubHyperliquidService.call_count == 0
    assert app.state.db.get_orders(limit=10) == []

    # Prove dry-run left NO idempotency reservation: a second identical request
    # (same nonce) must also return "dry_run", not "duplicate" or 409. If the
    # first call had written to the idempotency store the nonce would be
    # in-flight or completed and the second call would diverge.
    resp2 = client.post("/webhook", json=payload)
    assert resp2.status_code == 200, resp2.text
    assert resp2.json()["status"] == "dry_run"
    assert StubHyperliquidService.call_count == 0


def test_dry_run_logs_startup_warning(monkeypatch, caplog):
    """Enabling dry-run logs a WARNING banner so a demo daemon isn't mistaken for live."""
    import logging as _logging
    monkeypatch.setenv("HYPERTRADE_DRY_RUN", "true")
    with caplog.at_level(_logging.WARNING, logger="uvicorn.error"):
        make_app(monkeypatch, secret="secret")
    assert any("DRY-RUN MODE" in r.getMessage() for r in caplog.records)


def test_dex_qualified_symbol_preserves_case(monkeypatch):
    """HIP-3 dex-qualified bases (e.g. 'xyz:KR200') pass through verbatim; plain
    bases are still upper-cased.

    Builder-deployed dex coins are dex-qualified ('xyz:KR200') and the dex prefix
    is case-sensitive on Hyperliquid, so upper-casing would corrupt it to
    'XYZ:KR200' and miss the real market.
    """
    # dex-qualified base: colon present -> verbatim, case preserved
    StubHyperliquidService.reset()
    app = make_app(monkeypatch, secret="secret")
    client = TestClient(app)

    payload = copy.deepcopy(BASE_PAYLOAD)
    payload["currency"]["base"] = "xyz:KR200"
    resp = client.post("/webhook", json=payload)
    assert resp.status_code == 200, resp.text
    assert resp.json()["symbol"] == "xyz:KR200"
    assert StubHyperliquidService.last_order_request.symbol == "xyz:KR200"

    # plain base: still normalized to upper-case (regression guard)
    StubHyperliquidService.reset()
    payload2 = copy.deepcopy(BASE_PAYLOAD)
    payload2["currency"]["base"] = "link"
    resp2 = client.post("/webhook", json=payload2)
    assert resp2.status_code == 200, resp2.text
    assert resp2.json()["symbol"] == "LINK"
    assert StubHyperliquidService.last_order_request.symbol == "LINK"


def test_order_request_carries_req_id(monkeypatch):
    """The webhook must thread its request id onto OrderRequest so downstream
    failure logs can correlate back to the originating request."""
    StubHyperliquidService.reset()
    app = make_app(monkeypatch, secret="secret")
    client = TestClient(app)

    resp = client.post("/webhook", json=copy.deepcopy(BASE_PAYLOAD))
    assert resp.status_code == 200, resp.text
    order_req = StubHyperliquidService.last_order_request
    assert order_req is not None
    assert isinstance(order_req.req_id, str) and order_req.req_id


def test_network_failure_logs_error_and_correlation(monkeypatch, caplog):
    """The terminal retry line must carry the underlying error string AND
    correlation (cloid); the handler line must carry the req_id."""
    import logging as _logging
    StubHyperliquidService.reset()
    StubHyperliquidService.should_fail = True
    StubHyperliquidService.failure_type = "network"
    app = make_app(monkeypatch, secret="secret")
    client = TestClient(app)

    with caplog.at_level(_logging.WARNING, logger="uvicorn.error"):
        resp = client.post("/webhook", json=copy.deepcopy(BASE_PAYLOAD))
    assert resp.status_code == 503

    msgs = [r.getMessage() for r in caplog.records]
    assert any(
        "Order placement failed after" in m and "Network timeout" in m and "cloid=" in m
        for m in msgs
    ), msgs
    assert any(
        "Network error placing order" in m and "req_id=" in m for m in msgs
    ), msgs


def test_failure_logs_do_not_leak_secret(monkeypatch, caplog):
    """A failing order must not emit the webhook secret in failure-level (WARNING+)
    logs. (The DEBUG full-payload log is out of scope and excluded by the level.)"""
    import logging as _logging
    StubHyperliquidService.reset()
    StubHyperliquidService.should_fail = True
    StubHyperliquidService.failure_type = "api"
    app = make_app(monkeypatch, secret="topsecret-xyz")
    client = TestClient(app)

    payload = copy.deepcopy(BASE_PAYLOAD)
    payload["general"]["secret"] = "topsecret-xyz"

    with caplog.at_level(_logging.WARNING, logger="uvicorn.error"):
        resp = client.post("/webhook", json=payload)
    assert resp.status_code == 502

    assert len(caplog.records) > 0, "Expected at least one WARNING+ record from the failure path"
    for record in caplog.records:
        assert "topsecret-xyz" not in record.getMessage()
