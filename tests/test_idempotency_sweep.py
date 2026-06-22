"""Tests for the age-based sweep of completed idempotency nonces (TD-4).

Completed nonces accumulate forever unless swept, so every reserve() pays an
ever-larger index. These tests pin the sweep behaviour: old completed rows are
removed; recent completed rows and any in_progress rows survive.
"""

from __future__ import annotations

import pathlib
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

REPO_ROOT = str(pathlib.Path(__file__).resolve().parents[1])
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from hypertrade.idempotency import IdempotencyStore, ReserveOutcome


def _store(tmp_path):
    return IdempotencyStore(str(tmp_path / "idem.db"))


def _iso_ago(seconds: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


def _insert(store, nonce, status, *, created_at, completed_at=None, result_json=None):
    conn = sqlite3.connect(store.db_path)
    try:
        with conn:
            conn.execute(
                "INSERT INTO idempotency_keys "
                "(nonce, status, request_id, result_json, created_at, completed_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (nonce, status, "req", result_json, created_at, completed_at),
            )
    finally:
        conn.close()


def _nonces(store):
    conn = sqlite3.connect(store.db_path)
    try:
        rows = conn.execute("SELECT nonce FROM idempotency_keys").fetchall()
    finally:
        conn.close()
    return {r[0] for r in rows}


def test_sweep_removes_old_completed_keeps_recent_and_in_progress(tmp_path, monkeypatch):
    store = _store(tmp_path)
    retention = 100

    # Force a deterministic retention window.
    from hypertrade.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "idempotency_retention_seconds", retention, raising=False)

    # Old completed: completed_at well past the retention window -> must be swept.
    _insert(
        store,
        "old_completed",
        "completed",
        created_at=_iso_ago(retention + 1000),
        completed_at=_iso_ago(retention + 500),
        result_json='{"v": "old"}',
    )
    # Recent completed: completed_at inside the retention window -> must survive.
    _insert(
        store,
        "recent_completed",
        "completed",
        created_at=_iso_ago(5),
        completed_at=_iso_ago(1),
        result_json='{"v": "recent"}',
    )
    # In-progress, even if old: NEVER swept by this path (reclaim handles it).
    _insert(
        store,
        "old_in_progress",
        "in_progress",
        created_at=_iso_ago(retention + 1000),
    )

    store._sweep_completed()

    remaining = _nonces(store)
    assert "old_completed" not in remaining
    assert "recent_completed" in remaining
    assert "old_in_progress" in remaining


def test_reserve_triggers_sweep(tmp_path, monkeypatch):
    store = _store(tmp_path)
    retention = 100

    from hypertrade.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "idempotency_retention_seconds", retention, raising=False)

    _insert(
        store,
        "old_completed",
        "completed",
        created_at=_iso_ago(retention + 1000),
        completed_at=_iso_ago(retention + 500),
        result_json='{"v": "old"}',
    )

    # A normal reserve of a brand-new nonce must opportunistically sweep.
    assert store.reserve("fresh", "req-fresh", 60).outcome is ReserveOutcome.NEW

    remaining = _nonces(store)
    assert "old_completed" not in remaining
    assert "fresh" in remaining


def test_sweep_failure_does_not_propagate_to_reserve(tmp_path, monkeypatch):
    store = _store(tmp_path)

    def _boom() -> None:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(store, "_sweep_completed", _boom)

    # reserve must still succeed even though the best-effort sweep blew up.
    assert store.reserve("n1", "req1", 60).outcome is ReserveOutcome.NEW


def test_retention_default_is_seven_days():
    from hypertrade.config import get_settings

    assert get_settings().idempotency_retention_seconds == 604800
