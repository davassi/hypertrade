"""Unit tests for the persistent idempotency store."""

from __future__ import annotations

import pathlib
import sys

REPO_ROOT = str(pathlib.Path(__file__).resolve().parents[1])
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from hypertrade.idempotency import IdempotencyStore, ReserveOutcome


def _store(tmp_path):
    return IdempotencyStore(str(tmp_path / "idem.db"))


def test_reserve_new_nonce(tmp_path):
    store = _store(tmp_path)
    assert store.reserve("n1", "req1", 60).outcome is ReserveOutcome.NEW


def test_reserve_in_flight_when_not_stale(tmp_path):
    store = _store(tmp_path)
    store.reserve("n1", "req1", 60)
    assert store.reserve("n1", "req2", 60).outcome is ReserveOutcome.IN_FLIGHT


def test_completed_nonce_returns_stored_result(tmp_path):
    store = _store(tmp_path)
    store.reserve("n1", "req1", 60)
    store.complete("n1", {"status": "ok", "order_id": "abc"})
    r = store.reserve("n1", "req2", 60)
    assert r.outcome is ReserveOutcome.DUPLICATE_COMPLETED
    assert r.result == {"status": "ok", "order_id": "abc"}


def test_released_nonce_is_reservable_again(tmp_path):
    store = _store(tmp_path)
    store.reserve("n1", "req1", 60)
    store.release("n1")
    assert store.reserve("n1", "req2", 60).outcome is ReserveOutcome.NEW


def test_stale_in_progress_is_reclaimed(tmp_path):
    store = _store(tmp_path)
    store.reserve("n1", "req1", 60)
    # timeout of 0 makes any prior reservation stale
    assert store.reserve("n1", "req2", 0).outcome is ReserveOutcome.NEW
