"""Persistent idempotency store for at-most-once webhook order placement.

Keyed on an opaque, sender-provided nonce. Reserve before placing an order;
complete on success (the nonce then blocks future duplicates) or release on
failure (the nonce becomes reservable again so a retry can re-attempt).
"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class ReserveOutcome(Enum):
    NEW = "new"                        # reserved; caller must place the order
    DUPLICATE_COMPLETED = "completed"  # already done; replay stored result
    IN_FLIGHT = "in_flight"            # another reservation active and not stale


@dataclass
class ReserveResult:
    outcome: ReserveOutcome
    result: Optional[dict] = None      # set when DUPLICATE_COMPLETED


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _age_seconds(created_at_iso: str, now_iso: str) -> Optional[float]:
    try:
        created = datetime.fromisoformat(created_at_iso)
        now = datetime.fromisoformat(now_iso)
        return (now - created).total_seconds()
    except ValueError:
        return None


class IdempotencyStore:
    """SQLite-backed reserve/complete/release store keyed on a nonce."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._ensure_schema()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        conn = self._get_connection()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS idempotency_keys (
                    nonce        TEXT PRIMARY KEY,
                    status       TEXT NOT NULL,
                    request_id   TEXT,
                    result_json  TEXT,
                    created_at   TEXT NOT NULL,
                    completed_at TEXT
                )
                """
            )
            conn.commit()
        finally:
            conn.close()

    def reserve(self, nonce: str, request_id: str, inflight_timeout_s: int) -> ReserveResult:
        now = _now_iso()
        conn = self._get_connection()
        try:
            with conn:  # single transaction
                try:
                    conn.execute(
                        "INSERT INTO idempotency_keys (nonce, status, request_id, created_at) "
                        "VALUES (?, 'in_progress', ?, ?)",
                        (nonce, request_id, now),
                    )
                    return ReserveResult(ReserveOutcome.NEW)
                except sqlite3.IntegrityError:
                    pass  # nonce already present; inspect existing row

                row = conn.execute(
                    "SELECT status, result_json, created_at "
                    "FROM idempotency_keys WHERE nonce = ?",
                    (nonce,),
                ).fetchone()
                if row is None:
                    return ReserveResult(ReserveOutcome.IN_FLIGHT)
                if row["status"] == "completed":
                    result = json.loads(row["result_json"]) if row["result_json"] else None
                    return ReserveResult(ReserveOutcome.DUPLICATE_COMPLETED, result)

                age = _age_seconds(row["created_at"], now)
                if age is not None and age > inflight_timeout_s:
                    reclaimed = conn.execute(
                        "UPDATE idempotency_keys SET created_at = ?, request_id = ? "
                        "WHERE nonce = ? AND status = 'in_progress' AND created_at = ?",
                        (now, request_id, nonce, row["created_at"]),
                    ).rowcount
                    if reclaimed == 1:
                        return ReserveResult(ReserveOutcome.NEW)
                return ReserveResult(ReserveOutcome.IN_FLIGHT)
        finally:
            conn.close()

    def complete(self, nonce: str, result: dict) -> None:
        conn = self._get_connection()
        try:
            with conn:
                conn.execute(
                    "UPDATE idempotency_keys "
                    "SET status = 'completed', result_json = ?, completed_at = ? "
                    "WHERE nonce = ? AND status = 'in_progress'",
                    (json.dumps(result), _now_iso(), nonce),
                )
        finally:
            conn.close()

    def release(self, nonce: str) -> None:
        conn = self._get_connection()
        try:
            with conn:
                conn.execute(
                    "DELETE FROM idempotency_keys "
                    "WHERE nonce = ? AND status = 'in_progress'",
                    (nonce,),
                )
        finally:
            conn.close()
