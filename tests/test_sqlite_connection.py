"""Tests for the shared SQLite connection factory.

The webhook handler offloads order placement onto real OS threads via
``asyncio.to_thread``; each thread opens its own write connection to the same
SQLite file. Without WAL and a non-zero ``busy_timeout`` the first writer
collision raises ``database is locked`` immediately. These tests pin the two
PRAGMAs that fix that, plus a deterministic concurrency smoke test.
"""

from __future__ import annotations

import pathlib
import sys
import threading

REPO_ROOT = str(pathlib.Path(__file__).resolve().parents[1])
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from hypertrade import sqlite_util
from hypertrade.idempotency import IdempotencyStore


def test_connection_uses_wal_journal_mode(tmp_path):
    conn = sqlite_util.connect(str(tmp_path / "factory.db"))
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()
    assert str(mode).lower() == "wal"


def test_connection_sets_busy_timeout(tmp_path):
    conn = sqlite_util.connect(str(tmp_path / "factory.db"))
    try:
        busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    finally:
        conn.close()
    assert busy_timeout == sqlite_util.BUSY_TIMEOUT_MS == 5000


def test_concurrent_reserve_complete_no_lock(tmp_path):
    """Many OS threads hammering one store must not raise 'database is locked'."""
    store = IdempotencyStore(str(tmp_path / "idem.db"))
    errors: list[BaseException] = []
    barrier = threading.Barrier(8)

    def worker(i: int) -> None:
        nonce = f"n{i}"
        try:
            barrier.wait()  # release all threads simultaneously to force contention
            store.reserve(nonce, f"req{i}", 60)
            store.complete(nonce, {"status": "ok", "i": i})
            store.release(nonce)
        except BaseException as exc:  # noqa: BLE001 - test must surface any failure
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent access raised: {errors!r}"
