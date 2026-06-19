# Bounded History Retention Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cap the `orders` and `failures` SQLite tables to the most recent N rows (default 200) via trim-on-insert, ending unbounded growth / disk saturation.

**Architecture:** A new `HYPERTRADE_MAX_HISTORY_ROWS` setting flows into `OrderDatabase(max_rows=...)`. After each INSERT, `log_order`/`log_failure` delete rows beyond the newest `max_rows` (by autoincrement `id`) inside the same transaction. Read endpoints are untouched.

**Tech Stack:** Python 3.10+, Pydantic v2 settings, sqlite3 (stdlib), pytest.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-06-19-bounded-history-retention-design.md`.
- `HYPERTRADE_MAX_HISTORY_ROWS` default **200**, validated `>= 1`.
- Cap applies **per table** to `orders` and `failures` only (NOT `idempotency_keys`).
- Trim on insert, same transaction as the insert; key on the `id` autoincrement column.
- Run tests with: `python3.11 -m pytest -p no:warnings -q`.
- Commit messages end with: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

---

### Task 1: Config — `max_history_rows` setting

**Files:**
- Modify: `hypertrade/config.py` (add field near `db_enabled` at `:101-102`; add validator near the other `field_validator`s, e.g. after `_validate_premium_bps`)
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `Settings.max_history_rows: int` (default `200`); constructing `Settings` with `max_history_rows <= 0` raises `ValueError`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_config.py`:

```python
def test_max_history_rows_defaults_to_200(monkeypatch) -> None:
    from hypertrade.config import Settings
    monkeypatch.setenv("HYPERTRADE_ENVIRONMENT", "test")
    monkeypatch.setenv("HYPERTRADE_MASTER_ADDR", "0xMASTER")
    monkeypatch.setenv("HYPERTRADE_API_WALLET_PRIV", "dummy-priv-key")
    monkeypatch.setenv("HYPERTRADE_WEBHOOK_SECRET", "secret")
    monkeypatch.delenv("HYPERTRADE_MAX_HISTORY_ROWS", raising=False)
    assert Settings(_env_file=None).max_history_rows == 200


def test_max_history_rows_must_be_positive(monkeypatch) -> None:
    import pytest
    from hypertrade.config import Settings
    monkeypatch.setenv("HYPERTRADE_ENVIRONMENT", "test")
    monkeypatch.setenv("HYPERTRADE_MASTER_ADDR", "0xMASTER")
    monkeypatch.setenv("HYPERTRADE_API_WALLET_PRIV", "dummy-priv-key")
    monkeypatch.setenv("HYPERTRADE_WEBHOOK_SECRET", "secret")
    monkeypatch.setenv("HYPERTRADE_MAX_HISTORY_ROWS", "0")
    with pytest.raises(ValueError, match="max_history_rows"):
        Settings(_env_file=None)
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3.11 -m pytest tests/test_config.py -p no:warnings -q`
Expected: FAIL (`max_history_rows` attribute missing / no ValueError raised).

- [ ] **Step 3: Add the field** — in `hypertrade/config.py`, immediately after the `db_enabled` field (`:102`):

```python
    # Cap orders/failures history tables to the most recent N rows (trim-on-insert)
    max_history_rows: int = 200
```

- [ ] **Step 4: Add the validator** — in `hypertrade/config.py`, alongside the other `@field_validator` methods (e.g. right after `_validate_premium_bps`):

```python
    @field_validator("max_history_rows")
    @classmethod
    def _validate_max_history_rows(cls, value: int) -> int:
        """Must keep at least one row; <= 0 would delete everything on insert."""
        if value < 1:
            raise ValueError("max_history_rows must be at least 1")
        return value
```

- [ ] **Step 5: Run to verify they pass**

Run: `python3.11 -m pytest tests/test_config.py -p no:warnings -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add hypertrade/config.py tests/test_config.py
git commit -m "feat(config): add max_history_rows setting (default 200, >=1)"
```

---

### Task 2: DB trim-on-insert + daemon wiring

**Files:**
- Modify: `hypertrade/database.py` (`OrderDatabase.__init__` at `:17`; `log_order` insert block ending `:161`; `log_failure` insert block ending `:214`)
- Modify: `hypertrade/daemon.py` (the `OrderDatabase(settings.db_path)` construction at `:139`)
- Test: `tests/test_db_retention.py`

**Interfaces:**
- Consumes: `Settings.max_history_rows` (from Task 1).
- Produces: `OrderDatabase(db_path, max_rows: int = 200)`; after every `log_order`/`log_failure`, the respective table holds at most `max_rows` rows (the newest by `id`).

- [ ] **Step 1: Write the failing tests** — create `tests/test_db_retention.py`:

```python
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
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3.11 -m pytest tests/test_db_retention.py -p no:warnings -q`
Expected: FAIL (`OrderDatabase` has no `max_rows`; tables not capped — counts are 5 not 3/2).

- [ ] **Step 3: Add `max_rows` to the constructor** — in `hypertrade/database.py`, change `OrderDatabase.__init__` (`:17`):

```python
    def __init__(self, db_path: str = "./hypertrade.db", max_rows: int = 200):
        """Initialize database connection and create tables if needed.

        Args:
            db_path: Path to SQLite database file
            max_rows: Max rows retained per history table (orders, failures)
        """
        self.db_path = db_path
        self.max_rows = max_rows
        self._ensure_db_exists()
```

- [ ] **Step 4: Trim in `log_order`** — in `hypertrade/database.py`, between the order INSERT's closing `))` (`:161`) and `conn.commit()` (`:162`), add the trim so it shares the insert's transaction:

```python
            ))
            cursor.execute(
                "DELETE FROM orders WHERE id NOT IN "
                "(SELECT id FROM orders ORDER BY id DESC LIMIT ?)",
                (self.max_rows,),
            )
            conn.commit()
```

- [ ] **Step 5: Trim in `log_failure`** — in `hypertrade/database.py`, between the failures INSERT's closing `))` (`:214`) and `conn.commit()` (`:215`), add:

```python
            ))
            cursor.execute(
                "DELETE FROM failures WHERE id NOT IN "
                "(SELECT id FROM failures ORDER BY id DESC LIMIT ?)",
                (self.max_rows,),
            )
            conn.commit()
```

- [ ] **Step 6: Run the retention tests to verify they pass**

Run: `python3.11 -m pytest tests/test_db_retention.py -p no:warnings -q`
Expected: PASS (4 tests).

- [ ] **Step 7: Wire the setting in the daemon** — in `hypertrade/daemon.py`, change the construction (`:139`):

```python
            db = OrderDatabase(settings.db_path, max_rows=settings.max_history_rows)
```

- [ ] **Step 8: Run the full suite (no regressions)**

Run: `python3.11 -m pytest -p no:warnings -q`
Expected: PASS (all tests).

- [ ] **Step 9: Commit**

```bash
git add hypertrade/database.py hypertrade/daemon.py tests/test_db_retention.py
git commit -m "feat(db): cap orders/failures to max_history_rows via trim-on-insert"
```

---

## Self-Review

**Spec coverage:**
- §3 trim-on-insert on orders + failures, keyed on `id` → Task 2 Steps 4-5. ✅
- §4 `HYPERTRADE_MAX_HISTORY_ROWS` default 200 → Task 1; `OrderDatabase(max_rows=...)` → Task 2 Step 3; daemon wiring → Task 2 Step 7. ✅
- §4 read endpoints unchanged → no task touches them. ✅
- §5 `max_rows >= 1` validation → Task 1 Step 4. ✅
- §6 tests (cap keeps newest, under-cap intact, failures capped, default) → Task 2 Step 1; config default + validation → Task 1 Step 1. ✅
- §2 non-goal: `idempotency_keys` untouched → no task references it. ✅

**Placeholder scan:** No TBD/TODO/"similar to"/vague-validation placeholders; every code step shows the exact code.

**Type consistency:** `max_history_rows` (settings) → `max_rows` (OrderDatabase param/attr) used consistently across Task 1, Task 2 Steps 3-5-7; `self.max_rows` referenced in both trim statements; test helpers use the same `OrderDatabase(max_rows=...)` signature.
