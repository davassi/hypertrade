# Webhook Idempotency Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Guarantee at-most-once Hyperliquid order placement per sender-provided `general.nonce`, gated by a startup flag.

**Architecture:** A persistent SQLite `idempotency_keys` table backs an isolated `IdempotencyStore` with a reserve→complete/release state machine. The webhook handler reserves the nonce before placing an order, completes it on success, and releases it on failure (so failures stay retryable). Enabled by default; requires the order DB.

**Tech Stack:** Python 3.10+, FastAPI, Pydantic v2, sqlite3 (stdlib), pytest.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-06-18-webhook-idempotency-design.md`.
- `HYPERTRADE_IDEMPOTENCY_ENABLED` default **`true`**; `HYPERTRADE_IDEMPOTENCY_INFLIGHT_TIMEOUT` default **`60`** (seconds).
- When enabled: `general.nonce` required (`400` if missing); order DB required (startup `ValueError` if `db_enabled` is false).
- Consume nonce **on success only**; release on placement failure.
- Single daemon process assumption for reserve atomicity.
- Run tests with: `python3.11 -m pytest -p no:warnings -q`.
- Commit messages end with: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

---

### Task 1: Config — settings + DB-required fail-fast

**Files:**
- Modify: `hypertrade/config.py` (add fields near `db_enabled` at `:101-102`; add validator after `_validate_webhook_authentication` at `:144-162`)
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `Settings.idempotency_enabled: bool` (default `True`), `Settings.idempotency_inflight_timeout: int` (default `60`); startup `ValueError` when `idempotency_enabled and not db_enabled`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_config.py`:

```python
def test_idempotency_enabled_defaults_true(monkeypatch) -> None:
    from hypertrade.config import Settings
    monkeypatch.setenv("HYPERTRADE_ENVIRONMENT", "test")
    monkeypatch.setenv("HYPERTRADE_MASTER_ADDR", "0xMASTER")
    monkeypatch.setenv("HYPERTRADE_API_WALLET_PRIV", "dummy-priv-key")
    monkeypatch.setenv("HYPERTRADE_WEBHOOK_SECRET", "secret")
    monkeypatch.delenv("HYPERTRADE_IDEMPOTENCY_ENABLED", raising=False)
    s = Settings(_env_file=None)
    assert s.idempotency_enabled is True
    assert s.idempotency_inflight_timeout == 60


def test_idempotency_enabled_requires_db(monkeypatch) -> None:
    from hypertrade.config import Settings
    monkeypatch.setenv("HYPERTRADE_ENVIRONMENT", "test")
    monkeypatch.setenv("HYPERTRADE_MASTER_ADDR", "0xMASTER")
    monkeypatch.setenv("HYPERTRADE_API_WALLET_PRIV", "dummy-priv-key")
    monkeypatch.setenv("HYPERTRADE_WEBHOOK_SECRET", "secret")
    monkeypatch.setenv("HYPERTRADE_IDEMPOTENCY_ENABLED", "true")
    monkeypatch.setenv("HYPERTRADE_DB_ENABLED", "false")
    import pytest
    with pytest.raises(ValueError, match="requires the order DB"):
        Settings(_env_file=None)
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3.11 -m pytest tests/test_config.py -p no:warnings -q`
Expected: FAIL (`idempotency_enabled` attribute missing / no ValueError raised).

- [ ] **Step 3: Add the settings fields** — in `hypertrade/config.py`, after the `db_enabled` field (`:102`):

```python
    # Idempotency (at-most-once order placement keyed on general.nonce)
    idempotency_enabled: bool = True
    idempotency_inflight_timeout: int = 60  # seconds before an in_progress reservation is reclaimable
```

- [ ] **Step 4: Add the fail-fast validator** — in `hypertrade/config.py`, after `_validate_webhook_authentication` returns `self` (`:162`):

```python
    @model_validator(mode="after")
    def _validate_idempotency_requires_db(self):
        """Idempotency needs the order DB as its dedup store."""
        if self.idempotency_enabled and not self.db_enabled:
            raise ValueError(
                "HYPERTRADE_IDEMPOTENCY_ENABLED=true requires the order DB "
                "(HYPERTRADE_DB_ENABLED=true) as the dedup store."
            )
        return self
```

- [ ] **Step 5: Run to verify they pass**

Run: `python3.11 -m pytest tests/test_config.py -p no:warnings -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add hypertrade/config.py tests/test_config.py
git commit -m "feat(config): idempotency settings + DB-required fail-fast"
```

---

### Task 2: `IdempotencyStore`

**Files:**
- Create: `hypertrade/idempotency.py`
- Test: `tests/test_idempotency.py`

**Interfaces:**
- Produces:
  - `class ReserveOutcome(Enum)`: `NEW`, `DUPLICATE_COMPLETED`, `IN_FLIGHT`.
  - `@dataclass ReserveResult`: `outcome: ReserveOutcome`, `result: Optional[dict] = None`.
  - `class IdempotencyStore`: `__init__(self, db_path: str)`, `reserve(self, nonce: str, request_id: str, inflight_timeout_s: int) -> ReserveResult`, `complete(self, nonce: str, result: dict) -> None`, `release(self, nonce: str) -> None`.

- [ ] **Step 1: Write the failing tests** — create `tests/test_idempotency.py`:

```python
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
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3.11 -m pytest tests/test_idempotency.py -p no:warnings -q`
Expected: FAIL (`ModuleNotFoundError: hypertrade.idempotency`).

- [ ] **Step 3: Implement the store** — create `hypertrade/idempotency.py`:

```python
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
                    "WHERE nonce = ?",
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
```

- [ ] **Step 4: Run to verify they pass**

Run: `python3.11 -m pytest tests/test_idempotency.py -p no:warnings -q`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add hypertrade/idempotency.py tests/test_idempotency.py
git commit -m "feat(idempotency): persistent reserve/complete/release store"
```

---

### Task 3: Payload `nonce` field (model + schema)

**Files:**
- Modify: `hypertrade/schemas/tradingview.py` (`General` model)
- Modify: `hypertrade/schemas/tradingview_schema.py` (`general.properties`)
- Test: `tests/test_webhook.py`

**Interfaces:**
- Produces: `TradingViewWebhook.general.nonce: Optional[str]` (defaults `None`); JSON schema accepts an optional `nonce` string in `general`.

- [ ] **Step 1: Write the failing test** — append to `tests/test_webhook.py`:

```python
def test_general_model_parses_nonce():
    from hypertrade.schemas.tradingview import TradingViewWebhook
    payload = copy.deepcopy(BASE_PAYLOAD)
    payload["general"]["nonce"] = "abc-123"
    model = TradingViewWebhook.model_validate(payload)
    assert model.general.nonce == "abc-123"
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3.11 -m pytest tests/test_webhook.py::test_general_model_parses_nonce -p no:warnings -q`
Expected: FAIL (`model.general.nonce` attribute error).

- [ ] **Step 3: Add `nonce` to the General model** — in `hypertrade/schemas/tradingview.py`, in `class General`, after `leverage`:

```python
    leverage: Optional[str] = None
    nonce: Optional[str] = None
```

- [ ] **Step 4: Add `nonce` to the JSON schema** — in `hypertrade/schemas/tradingview_schema.py`, inside `general.properties` after `leverage`:

```python
                "leverage": {"type": "string", "minLength": 1},
                "nonce": {"type": "string", "minLength": 1}
```

- [ ] **Step 5: Run to verify it passes**

Run: `python3.11 -m pytest tests/test_webhook.py::test_general_model_parses_nonce -p no:warnings -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add hypertrade/schemas/tradingview.py hypertrade/schemas/tradingview_schema.py tests/test_webhook.py
git commit -m "feat(schema): accept optional general.nonce"
```

---

### Task 4: Daemon wiring + test harness default-off

**Files:**
- Modify: `hypertrade/daemon.py` (after the db init block at `:136-147`)
- Modify: `tests/test_webhook.py` (`make_app` at `:99-110`)

**Interfaces:**
- Consumes: `Settings.idempotency_enabled`, `Settings.db_path`, `IdempotencyStore`.
- Produces: `app.state.idempotency` is an `IdempotencyStore` when enabled, else `None`.

> Default-on would make every existing nonce-less webhook test return `400`.
> `make_app` therefore disables idempotency by default; the Task 5 idempotency
> tests opt back in explicitly.

- [ ] **Step 1: Disable idempotency in the test harness** — in `tests/test_webhook.py` `make_app`, after the `PRIVATE_KEY` line (`:110`):

```python
    monkeypatch.setenv("PRIVATE_KEY", "0x" + "1" * 64)
    # Idempotency is opt-in per-test; keep the default suite nonce-free.
    if os.getenv("HYPERTRADE_IDEMPOTENCY_ENABLED") is None:
        monkeypatch.setenv("HYPERTRADE_IDEMPOTENCY_ENABLED", "false")
```

- [ ] **Step 2: Verify the existing suite still passes** (regression guard for default-off harness)

Run: `python3.11 -m pytest -p no:warnings -q`
Expected: PASS (all currently-passing tests).

- [ ] **Step 3: Wire the store into the daemon** — in `hypertrade/daemon.py`, after the db init block (`:147`, after the `else: app.state.db = None` branch):

```python
    # Idempotency store (shares the order DB file). Config guarantees db_enabled
    # is true whenever idempotency is enabled (see Settings validator).
    if settings.idempotency_enabled:
        from .idempotency import IdempotencyStore
        app.state.idempotency = IdempotencyStore(settings.db_path)
        log.info(
            "Idempotency enabled (in-flight timeout %ss)",
            settings.idempotency_inflight_timeout,
        )
    else:
        app.state.idempotency = None
        log.info("Idempotency disabled")
```

- [ ] **Step 4: Run the suite to confirm wiring imports cleanly**

Run: `python3.11 -m pytest -p no:warnings -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add hypertrade/daemon.py tests/test_webhook.py
git commit -m "feat(idempotency): wire store into daemon; default-off test harness"
```

---

### Task 5: Handler integration (reserve / duplicate / complete / release)

**Files:**
- Modify: `hypertrade/routes/webhooks.py` (handler `hypertrade_webhook`, `:85-316`; imports `:24`)
- Test: `tests/test_webhook.py`

**Interfaces:**
- Consumes: `app.state.idempotency` (`IdempotencyStore` or `None`), `ReserveOutcome`, `Settings.idempotency_inflight_timeout`, `payload.general.nonce`.
- Produces: webhook behavior — `400` missing nonce (enabled), `409` in-flight, `200 {"status":"duplicate", ...}` completed duplicate, single placement per nonce, release on failure.

- [ ] **Step 1: Write the failing integration tests** — append to `tests/test_webhook.py`:

```python
def _idem_payload(nonce):
    payload = copy.deepcopy(BASE_PAYLOAD)
    payload["general"]["nonce"] = nonce
    return payload


def test_idempotency_missing_nonce_returns_400(monkeypatch):
    monkeypatch.setenv("HYPERTRADE_IDEMPOTENCY_ENABLED", "true")
    StubHyperliquidService.reset()
    app = make_app(monkeypatch, secret="secret")
    client = TestClient(app)
    resp = client.post("/webhook", json=copy.deepcopy(BASE_PAYLOAD))  # no nonce
    assert resp.status_code == 400


def test_idempotency_duplicate_places_order_once(monkeypatch):
    monkeypatch.setenv("HYPERTRADE_IDEMPOTENCY_ENABLED", "true")
    StubHyperliquidService.reset()
    app = make_app(monkeypatch, secret="secret")
    client = TestClient(app)
    payload = _idem_payload("nonce-dup-1")

    first = client.post("/webhook", json=payload)
    assert first.status_code == 200
    assert StubHyperliquidService.call_count == 1

    second = client.post("/webhook", json=payload)
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate"
    assert StubHyperliquidService.call_count == 1  # not placed again


def test_idempotency_release_on_failure_allows_retry(monkeypatch):
    monkeypatch.setenv("HYPERTRADE_IDEMPOTENCY_ENABLED", "true")
    StubHyperliquidService.reset()
    StubHyperliquidService.should_fail = True
    StubHyperliquidService.failure_type = "network"
    app = make_app(monkeypatch, secret="secret")
    client = TestClient(app)
    payload = _idem_payload("nonce-retry-1")

    failed = client.post("/webhook", json=payload)
    assert failed.status_code == 503  # network error after retries

    # nonce released -> a retry with the same nonce succeeds and places the order
    StubHyperliquidService.should_fail = False
    ok = client.post("/webhook", json=payload)
    assert ok.status_code == 200
    assert ok.json()["status"] == "ok"
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3.11 -m pytest tests/test_webhook.py -k idempotency -p no:warnings -q`
Expected: FAIL (no nonce enforcement / duplicate re-places order).

- [ ] **Step 3: Import the store enum** — in `hypertrade/routes/webhooks.py`, after the enums import (`:24`):

```python
from ..idempotency import ReserveOutcome
```

- [ ] **Step 4: Validate nonce presence** — in `hypertrade/routes/webhooks.py`, immediately after `payload = TradingViewWebhook.model_validate(raw)` (`:103`):

```python
    idempotency = getattr(request.app.state, "idempotency", None)
    nonce = payload.general.nonce
    if idempotency is not None and not nonce:
        raise HTTPException(status_code=400, detail="general.nonce is required")
```

- [ ] **Step 5: Reserve before placing the order** — in `hypertrade/routes/webhooks.py`, after `req_id = getattr(request.state, "request_id", None)` (currently `:198`) and before the `try:` that calls `_place_order_with_retry`:

```python
    if idempotency is not None:
        reservation = idempotency.reserve(
            nonce, req_id, get_settings().idempotency_inflight_timeout
        )
        if reservation.outcome is ReserveOutcome.DUPLICATE_COMPLETED:
            return JSONResponse({"status": "duplicate", **(reservation.result or {})})
        if reservation.outcome is ReserveOutcome.IN_FLIGHT:
            raise HTTPException(status_code=409, detail="Duplicate request in flight")
```

- [ ] **Step 6: Release on failure via a flag + finally** — wrap the existing placement `try/except` block. Add `placed_ok = False` immediately before the `try:`, set `placed_ok = True` right after the `result = await _place_order_with_retry(...)` line, and add a `finally` after the last `except HyperliquidAPIError` block:

```python
    placed_ok = False
    try:
        log.info("Attempting to place order on Hyperliquid: symbol=%s side=%s", symbol, side.value)
        result = await _place_order_with_retry(client, order_request, max_retries=2)
        placed_ok = True
    except HyperliquidValidationError as e:
        # ... existing body unchanged ...
        raise HTTPException(status_code=400, detail=f"Invalid order: {e}") from e
    except HyperliquidNetworkError as e:
        # ... existing body unchanged ...
        raise HTTPException(status_code=503, detail="Temporary service unavailable - order may have been placed, check manually") from e
    except HyperliquidAPIError as e:
        # ... existing body unchanged ...
        raise HTTPException(status_code=502, detail=f"Exchange error: {e}") from e
    finally:
        if idempotency is not None and not placed_ok:
            idempotency.release(nonce)
```

- [ ] **Step 7: Complete on success** — in `hypertrade/routes/webhooks.py`, immediately after `response = _build_response(payload, signal=signal, side=side, symbol=symbol)` (currently `:299`):

```python
    if idempotency is not None:
        idempotency.complete(nonce, response)
```

- [ ] **Step 8: Run idempotency tests to verify they pass**

Run: `python3.11 -m pytest tests/test_webhook.py -k idempotency -p no:warnings -q`
Expected: PASS (3 tests).

- [ ] **Step 9: Run the full suite**

Run: `python3.11 -m pytest -p no:warnings -q`
Expected: PASS (all tests).

- [ ] **Step 10: Commit**

```bash
git add hypertrade/routes/webhooks.py tests/test_webhook.py
git commit -m "feat(webhook): enforce idempotency on order placement"
```

---

### Task 6: Documentation

**Files:**
- Modify: `docs/tradingview-webhook.md`
- Modify: `README.md`

**Interfaces:** none (docs only).

- [ ] **Step 1: Update the webhook reference** — in `docs/tradingview-webhook.md`:
  - In the §1 field table, add a `general` row: `nonce` — `➖` (required only when idempotency is enabled) — string — "Idempotency key; unique per order, reused on retry."
  - Add a short "## Idempotency" section: when `HYPERTRADE_IDEMPOTENCY_ENABLED` is true (default), every order-placing request must include `general.nonce`; missing → `400`, in-flight duplicate → `409`, completed duplicate → `200 {"status":"duplicate", ...}`.
  - Add `"nonce": "<unique-per-order>"` to the `general` block of each example payload.

- [ ] **Step 2: Update the README** — in `README.md`, in the security/limits area, add bullets:

```markdown
- `HYPERTRADE_IDEMPOTENCY_ENABLED` (default `true`): require a unique `general.nonce` per order and place each at most once. Requires the order DB (the daemon refuses to start if `HYPERTRADE_DB_ENABLED=false`).
- `HYPERTRADE_IDEMPOTENCY_INFLIGHT_TIMEOUT` (default `60`): seconds before an in-progress reservation is considered stale and reclaimable.
```

- [ ] **Step 3: Verify docs examples still validate** (guard against a malformed JSON edit)

Run:
```bash
python3.11 - <<'PY'
import re, json
text = open("docs/tradingview-webhook.md").read()
for b in re.findall(r"```json\n(.*?)\n```", text, re.DOTALL):
    json.loads(b)
print("all json blocks valid")
PY
```
Expected: `all json blocks valid`.

- [ ] **Step 4: Commit**

```bash
git add docs/tradingview-webhook.md README.md
git commit -m "docs: document webhook idempotency nonce + settings"
```

---

## Self-Review

**Spec coverage:**
- §3 config flags + DB fail-fast → Task 1. ✅
- §4 nonce contract / schema → Task 3. ✅
- §5 data model → Task 2 (`_ensure_schema`). ✅
- §6 `IdempotencyStore` API + semantics → Task 2. ✅
- §7 request flow → Task 5 (presence, reserve, complete, release). ✅
- §8 HTTP responses (400/409/200-duplicate) → Task 5 tests. ✅
- §9 docs updates → Task 6. ✅
- §10 error handling (release on failure, finally) → Task 5 Step 6. ✅
- §11 testing → Tasks 2 & 5. ✅
- Daemon wiring (implied by §3/§6) → Task 4. ✅

**Placeholder scan:** Task 5 Steps 6 references "existing body unchanged" for the three `except` blocks — these are the current `db.log_order(...)` / `db.log_failure(...)` bodies at `webhooks.py:204-275`; the only edits are adding `placed_ok = True` and the `finally`. No other placeholders.

**Type consistency:** `reserve`/`complete`/`release`, `ReserveOutcome.{NEW,DUPLICATE_COMPLETED,IN_FLIGHT}`, `ReserveResult.result`, `app.state.idempotency`, `general.nonce`, `idempotency_inflight_timeout` are used identically across Tasks 1–5.
