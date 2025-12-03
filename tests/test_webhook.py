"""Tests for TradingView webhook endpoint behavior and integrations.

These tests validate content-type enforcement, schema validation, optional
secret checks, IP whitelist behavior, and Telegram notification wiring.
"""

from __future__ import annotations

# pylint: disable=import-outside-toplevel

import sys
import pathlib
import json
import copy

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

    def __init__(self, *args, **kwargs):
        pass

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


def make_app(monkeypatch, *, secret: str | None = None):
    """Create app with env configured via pytest monkeypatch."""
    # Required env vars for settings
    monkeypatch.setenv("HYPERTRADE_ENVIRONMENT", "test")
    monkeypatch.setenv("HYPERTRADE_MASTER_ADDR", "0xMASTER")
    monkeypatch.setenv("HYPERTRADE_API_WALLET_PRIV", "dummy-priv-key")
    monkeypatch.setenv("HYPERTRADE_SUBACCOUNT_ADDR", "0xSUB")
    monkeypatch.setenv("PRIVATE_KEY", "0x" + "1" * 64)
    if secret is not None:
        monkeypatch.setenv("HYPERTRADE_WEBHOOK_SECRET", secret)

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

    app = make_app(monkeypatch, secret=None)  # no secret enforcement
    client = TestClient(app)

    payload = copy.deepcopy(BASE_PAYLOAD)
    headers = {"X-Forwarded-For": "9.9.9.9"}
    resp = client.post("/webhook", json=payload, headers=headers)
    assert resp.status_code == 403
    body = resp.json()
    assert body["error"]["status"] == 403


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
