from __future__ import annotations

import copy
from fastapi.testclient import TestClient
import sys
import pathlib
import json


BASE_PAYLOAD = {
    "general": {
        "strategy": "Solana Enhanced Strategy (114, 21, 1, 2, 0)",
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
    import hypertrade.daemon as daemon

    daemon.get_settings.cache_clear()
    app = daemon.create_daemon()
    return app


def test_webhook_happy_path_ok(monkeypatch):
    app = make_app(monkeypatch, secret="secret")
    client = TestClient(app)

    payload = copy.deepcopy(BASE_PAYLOAD)
    resp = client.post("/webhook", json=payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "ok"
    assert data["ticker"] == "SOLUSD"
    assert data["exchange"] == "COINBASE"
    assert data["action"] == "buy"


def test_webhook_rejects_bad_secret(monkeypatch):
    app = make_app(monkeypatch, secret="expected-secret")
    client = TestClient(app)

    payload = copy.deepcopy(BASE_PAYLOAD)
    # Payload carries a different secret than env
    payload["general"]["secret"] = "wrong-secret"
    resp = client.post("/webhook", json=payload)
    assert resp.status_code == 401
    body = resp.json()
    assert body["error"]["status"] == 401


def test_webhook_ip_whitelist_allows_forwarded(monkeypatch):
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
    # Enable Telegram settings
    monkeypatch.setenv("HYPERTRADE_TELEGRAM_BOT_TOKEN", "TEST_TOKEN")
    monkeypatch.setenv("HYPERTRADE_TELEGRAM_CHAT_ID", "123456")

    # Provide a fake telebot module so no network is used
    import types
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
    # Ensure no telegram env is set
    for k in ("HYPERTRADE_TELEGRAM_BOT_TOKEN", "HYPERTRADE_TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(k, raising=False)

    # Spy on send_telegram_message to ensure not called
    import hypertrade.notify as notify
    called = {"n": 0}
    def fake_send(*args, **kwargs):
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
    # Set env vars but disable via flag
    monkeypatch.setenv("HYPERTRADE_TELEGRAM_BOT_TOKEN", "TEST_TOKEN")
    monkeypatch.setenv("HYPERTRADE_TELEGRAM_CHAT_ID", "123456")
    monkeypatch.setenv("HYPERTRADE_TELEGRAM_ENABLED", "false")

    # Spy to ensure not called when disabled
    import hypertrade.notify as notify
    called = {"n": 0}
    def fake_send(*args, **kwargs):
        called["n"] += 1
        return True
    monkeypatch.setattr(notify, "send_telegram_message", fake_send)

    app = make_app(monkeypatch, secret=None)
    client = TestClient(app)

    payload = copy.deepcopy(BASE_PAYLOAD)
    resp = client.post("/webhook", json=payload)
    assert resp.status_code == 200
    assert called["n"] == 0
