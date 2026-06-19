"""Bounded retention: orders/failures tables are capped to max_rows on insert."""

from __future__ import annotations

import pathlib
import sys

REPO_ROOT = str(pathlib.Path(__file__).resolve().parents[1])
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from hypertrade.database import OrderDatabase


def _count(db: OrderDatabase, table: str) -> int:
    conn = db._get_connection()
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def _order_request_ids(db: OrderDatabase) -> list[str]:
    conn = db._get_connection()
    try:
        return [r["request_id"] for r in conn.execute("SELECT request_id FROM orders ORDER BY id")]
    finally:
        conn.close()


def _log_order(db: OrderDatabase, rid: str) -> None:
    db.log_order(
        request_id=rid, symbol="SOL", side="buy", signal="OPEN_LONG",
        quantity=1, price=100, status="PLACED",
    )


def test_default_max_rows_is_200(tmp_path):
    assert OrderDatabase(str(tmp_path / "h.db")).max_rows == 200


def test_orders_capped_to_max_rows_keeping_newest(tmp_path):
    db = OrderDatabase(str(tmp_path / "h.db"), max_rows=3)
    for i in range(5):
        _log_order(db, f"r{i}")
    assert _count(db, "orders") == 3
    assert _order_request_ids(db) == ["r2", "r3", "r4"]  # newest 3 survive


def test_under_cap_keeps_all_orders(tmp_path):
    db = OrderDatabase(str(tmp_path / "h.db"), max_rows=10)
    for i in range(4):
        _log_order(db, f"r{i}")
    assert _count(db, "orders") == 4


def test_failures_capped_to_max_rows(tmp_path):
    db = OrderDatabase(str(tmp_path / "h.db"), max_rows=2)
    for i in range(5):
        db.log_failure(request_id=f"r{i}", error_type="Net", error_message="boom")
    assert _count(db, "failures") == 2
