"""Version single-source: the FastAPI app's version must match hypertrade.version.

Guards against the daemon hardcoding a version string that drifts from
``hypertrade/version.py`` (the single source of truth).
"""

from __future__ import annotations

import pathlib
import sys

REPO_ROOT = str(pathlib.Path(__file__).resolve().parents[1])
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


def _build_app(monkeypatch):
    """Build the daemon app with the minimal valid env, with a clean settings cache."""
    monkeypatch.setenv("HYPERTRADE_ENVIRONMENT", "test")
    monkeypatch.setenv(
        "HYPERTRADE_MASTER_ADDR",
        "0x000000000000000000000000000000000000dEaD",
    )
    monkeypatch.setenv("HYPERTRADE_API_WALLET_PRIV", "0" * 63 + "1")
    monkeypatch.setenv("HYPERTRADE_WEBHOOK_SECRET", "x")
    monkeypatch.setenv("HYPERTRADE_DB_ENABLED", "false")
    monkeypatch.setenv("HYPERTRADE_IDEMPOTENCY_ENABLED", "false")

    from hypertrade import daemon

    daemon.get_settings.cache_clear()
    return daemon.create_daemon()


def test_app_version_matches_single_source(monkeypatch):
    """create_daemon() must wire FastAPI's version to hypertrade.version.__version__."""
    from hypertrade.version import __version__

    app = _build_app(monkeypatch)
    assert app.version == __version__
