"""Shared SQLite connection factory for the webhook executor's stores.

The webhook handler offloads order placement onto real OS threads via
``asyncio.to_thread``. Each thread opens its own write connection to the same
SQLite database file. With SQLite's default ``busy_timeout`` of 0 the first
writer collision returns ``SQLITE_BUSY`` immediately, surfacing as
``sqlite3.OperationalError: database is locked``.

Every store must therefore obtain its connections here so they share identical
concurrency settings:

* WAL journal mode lets readers proceed concurrently with a single writer.
* A non-zero ``busy_timeout`` makes a contended writer wait and retry instead of
  failing instantly.
"""

from __future__ import annotations

import sqlite3

# How long (milliseconds) a connection waits for a lock before raising
# ``database is locked``. Generous enough to absorb the brief write windows of
# the idempotency reserve/complete/release path under thread contention.
BUSY_TIMEOUT_MS = 5000


def connect(db_path: str) -> sqlite3.Connection:
    """Open a SQLite connection configured for concurrent thread access.

    Preserves the semantics the stores previously relied on (default
    ``check_same_thread``/``timeout``/``isolation_level``, plus a
    :class:`sqlite3.Row` row factory) and additionally enables WAL journaling
    and a non-zero busy timeout so concurrent writers wait instead of failing
    with ``database is locked``.

    Args:
        db_path: Filesystem path to the SQLite database file.

    Returns:
        A configured, open :class:`sqlite3.Connection`. The caller owns it and
        is responsible for closing it.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    # Errors here must propagate: a connection that cannot honour these PRAGMAs
    # would silently reintroduce the locking bug, so never swallow them.
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn
