"""Tests for health check endpoints."""

from __future__ import annotations

import sys
import pathlib
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient


def make_app(monkeypatch):
    """Create app with env configured via pytest monkeypatch."""
    # Required env vars for settings
    monkeypatch.setenv("HYPERTRADE_ENVIRONMENT", "test")
    monkeypatch.setenv("HYPERTRADE_MASTER_ADDR", "0xMASTER")
    monkeypatch.setenv("HYPERTRADE_API_WALLET_PRIV", "dummy-priv-key")
    monkeypatch.setenv("HYPERTRADE_SUBACCOUNT_ADDR", "0xSUB")
    monkeypatch.setenv("PRIVATE_KEY", "0x" + "1" * 64)

    # Ensure this repo's package is first on sys.path to avoid name collisions
    repo_root = str(pathlib.Path(__file__).resolve().parents[1])
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)

    # Import the app factory and clear cached settings
    from hypertrade import daemon

    daemon.get_settings.cache_clear()
    app = daemon.create_daemon()
    return app


def test_liveness_probe_always_returns_200(monkeypatch):
    """Liveness probe (/health) always returns 200 if service is running."""
    app = make_app(monkeypatch)
    client = TestClient(app)

    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "alive"


def test_readiness_probe_returns_balance(monkeypatch):
    """Readiness probe returns available balance in response."""
    app = make_app(monkeypatch)
    client = TestClient(app)

    with patch("hypertrade.routes.health.HyperliquidService") as mock_service_class:
        mock_client = Mock()
        mock_client.client.data.get_available_balance.return_value = 12500.75
        mock_service_class.return_value = mock_client

        resp = client.get("/ready")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ready"
        assert data["available_balance"] == 12500.75


def test_readiness_probe_with_hyperliquid_check_success(monkeypatch):
    """Readiness probe succeeds when Hyperliquid connectivity is available."""
    app = make_app(monkeypatch)
    client = TestClient(app)

    # Mock the HyperliquidService to simulate successful connectivity
    with patch("hypertrade.routes.health.HyperliquidService") as mock_service_class:
        mock_client = Mock()
        mock_client.client.data.get_available_balance.return_value = 10000.0
        mock_service_class.return_value = mock_client

        resp = client.get("/ready")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ready"
        assert data["available_balance"] == 10000.0
        # Verify that get_available_balance was called
        mock_client.client.data.get_available_balance.assert_called_once()


def test_readiness_probe_always_checks_hyperliquid(monkeypatch):
    """Readiness probe always checks Hyperliquid (no optional checking)."""
    app = make_app(monkeypatch)
    client = TestClient(app)

    # Mock the HyperliquidService
    with patch("hypertrade.routes.health.HyperliquidService") as mock_service_class:
        mock_client = Mock()
        mock_client.client.data.get_available_balance.return_value = 5000.0
        mock_service_class.return_value = mock_client

        resp = client.get("/ready")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ready"
        assert data["available_balance"] == 5000.0
        # Should always check Hyperliquid
        mock_client.client.data.get_available_balance.assert_called_once()


def test_readiness_probe_hyperliquid_unreachable(monkeypatch):
    """Readiness probe returns 503 when Hyperliquid is unreachable."""
    app = make_app(monkeypatch)
    client = TestClient(app)

    # Mock the HyperliquidService to raise an exception
    with patch("hypertrade.routes.health.HyperliquidService") as mock_service_class:
        mock_service_class.side_effect = ConnectionError("API unreachable")

        resp = client.get("/ready")
        assert resp.status_code == 503
        data = resp.json()
        assert "Service not ready" in data["error"]["detail"]
        assert "Hyperliquid connection failed" in data["error"]["detail"]


def test_readiness_probe_invalid_credentials(monkeypatch):
    """Readiness probe returns 503 when credentials are invalid."""
    app = make_app(monkeypatch)
    client = TestClient(app)

    # Mock the HyperliquidService to raise authentication error
    with patch("hypertrade.routes.health.HyperliquidService") as mock_service_class:
        mock_client = Mock()
        mock_client.client.data.get_available_balance.side_effect = ValueError("Invalid credentials")
        mock_service_class.return_value = mock_client

        resp = client.get("/ready")
        assert resp.status_code == 503
        data = resp.json()
        assert "Service not ready" in data["error"]["detail"]


def test_readiness_probe_no_query_params(monkeypatch):
    """Readiness probe doesn't accept query parameters (simplified API)."""
    app = make_app(monkeypatch)
    client = TestClient(app)

    with patch("hypertrade.routes.health.HyperliquidService") as mock_service_class:
        mock_client = Mock()
        mock_client.client.data.get_available_balance.return_value = 8500.5
        mock_service_class.return_value = mock_client

        # Query params should be ignored, endpoint always checks
        resp = client.get("/ready?extra=param")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ready"
        assert data["available_balance"] == 8500.5
