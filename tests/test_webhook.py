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
import types

from fastapi.testclient import TestClient


BASE_PAYLOAD = {
    "general": {
        "strategy": "Solana Super Cool Enhanced Strategy (114, 21, 1, 2, 0)",
        "ticker": "SOLUSD",
        "exchange": "COINBASE",
        "interval": "60",
        "time": "2025-10-21T06:00:00Z",
        "timenow": "2025-10-21T06:00:45Z",
        "secret": "secret",
        "leverage": "1X",
    },
    "symbol_data": {
        "open": "183.90",
        "close": "183.78",
        "high": "183.91",
        "low": "183.75",
        "volume": "257.55477845",
    },
    "currency": {"quote": "USD", "base": "SOL"},
    "position": {"position_size": "0"},
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


def make_app(monkeypatch, *, secret: str | None = None):
    """Create app with env configured via pytest monkeypatch."""
    # Required env vars for settings
    monkeypatch.setenv("HYPERTRADE_MASTER_ADDR", "0xMASTER")
    monkeypatch.setenv("HYPERTRADE_API_WALLET_PRIV", "dummy-priv-key")
    monkeypatch.setenv("HYPERTRADE_SUBACCOUNT_ADDR", "0xSUB")
    if secret is not None:
        monkeypatch.setenv("HYPERTRADE_WEBHOOK_SECRET", secret)

    # Ensure this repo's package is first on sys.path to avoid name collisions
    repo_root = str(pathlib.Path(__file__).resolve().parents[1])
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)

    # Import the app factory and clear cached settings via the imported module
    from hypertrade import daemon

    daemon.get_settings.cache_clear()
    app = daemon.create_daemon()
    return app


def test_webhook_happy_path_ok(monkeypatch):
    """Returns 200 with normalized summary for valid payload."""
    app = make_app(monkeypatch, secret="secret")
    client = TestClient(app)

    payload = copy.deepcopy(BASE_PAYLOAD)
    resp = client.post("/webhook", json=payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "ok"
    assert data["signal"] == "CLOSE_SHORT"
    assert data["side"] == "buy"
    assert data["symbol"] == "SOLUSD"
    assert data["ticker"] == "SOLUSD"
    assert data["exchange"] == "COINBASE"
    assert data["action"] == "buy"
    assert data["contracts"] == "46231.75300000"
    assert data["price"] == "183.81"

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


def test_telegram_notification_background_task(monkeypatch):
    """Sends Telegram message via background task when configured."""
    # Enable Telegram settings
    monkeypatch.setenv("HYPERTRADE_TELEGRAM_BOT_TOKEN", "TEST_TOKEN")
    monkeypatch.setenv("HYPERTRADE_TELEGRAM_CHAT_ID", "123456")

    # Provide a fake telebot module so no network is used
    calls = {"n": 0, "last": {}}

    class FakeBot:
        def __init__(self, token):
            calls["last"]["token"] = token
        def send_message(self, chat_id, text):
            calls["n"] += 1
            calls["last"].update({"chat_id": chat_id, "text": text})

    fake_telebot = types.SimpleNamespace(TeleBot=FakeBot)
    monkeypatch.setitem(sys.modules, "telebot", fake_telebot)

    app = make_app(monkeypatch, secret=None)
    client = TestClient(app)

    payload = copy.deepcopy(BASE_PAYLOAD)
    resp = client.post("/webhook", json=payload)
    assert resp.status_code == 200
    # BackgroundTasks should have executed and called our fake
    assert calls["n"] == 1
    assert calls["last"]["token"] == "TEST_TOKEN"
    assert calls["last"]["chat_id"] == "123456"
    assert "SOLUSD" in calls["last"]["text"]


def test_telegram_disabled_when_no_config(monkeypatch):
    """Does not attempt to send Telegram when not configured."""
    # Ensure no telegram env is set
    for k in ("HYPERTRADE_TELEGRAM_BOT_TOKEN", "HYPERTRADE_TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(k, raising=False)

    # Spy on send_telegram_message to ensure not called
    from hypertrade import notify
    called = {"n": 0}
    def fake_send(*_args, **_kwargs):
        called["n"] += 1
        return True
    monkeypatch.setattr(notify, "send_telegram_message", fake_send)

    app = make_app(monkeypatch, secret=None)
    client = TestClient(app)

    payload = copy.deepcopy(BASE_PAYLOAD)
    resp = client.post("/webhook", json=payload)
    assert resp.status_code == 200
    assert called["n"] == 0


def test_telegram_disabled_flag(monkeypatch):
    """Respects explicit disable flag and does not send messages."""
    # Set env vars but disable via flag
    monkeypatch.setenv("HYPERTRADE_TELEGRAM_BOT_TOKEN", "TEST_TOKEN")
    monkeypatch.setenv("HYPERTRADE_TELEGRAM_CHAT_ID", "123456")
    monkeypatch.setenv("HYPERTRADE_TELEGRAM_ENABLED", "false")

    # Spy to ensure not called when disabled
    from hypertrade import notify
    called = {"n": 0}
    def fake_send(*_args, **_kwargs):
        called["n"] += 1
        return True
    monkeypatch.setattr(notify, "send_telegram_message", fake_send)

    app = make_app(monkeypatch, secret=None)
    client = TestClient(app)

    payload = copy.deepcopy(BASE_PAYLOAD)
    resp = client.post("/webhook", json=payload)
    assert resp.status_code == 200
    assert called["n"] == 0
