# Dry-Run (Demo) Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a daemon-level `HYPERTRADE_DRY_RUN` switch that runs the full `/webhook` receive→parse→validate→signal-map pipeline and returns a `dry_run` response with no side effects (no Hyperliquid call, no DB write, no idempotency write, no Telegram).

**Architecture:** One bool setting on `Settings`. In the webhook handler, branch right after `OrderRequest` is built (maximum pipeline coverage) and before the exchange client is constructed, returning a new `_build_dry_run_response`. A startup WARNING banner makes the demo mode unmistakable.

**Tech Stack:** Python 3.10+, FastAPI, Pydantic v2 / pydantic-settings, pytest + `fastapi.testclient` (+ `caplog`).

## Global Constraints

- Spec: `docs/superpowers/specs/2026-06-21-dry-run-mode-design.md`.
- Switch: env var `HYPERTRADE_DRY_RUN` (Pydantic field `dry_run: bool = False`); default false ⇒ current behavior unchanged.
- Dry-run response carries `"status": "dry_run"` (new value alongside `ok`/`ignored`/`duplicate`).
- Side-effect-free: dry-run must NOT construct `HyperliquidService`, call `place_order`, write the DB, touch the idempotency store, or send Telegram.
- Thin executor: do NOT fabricate order ids / fill prices — only echo the request.
- Run tests with: `python3.11 -m pytest -p no:warnings -q`.
- Commit messages end with: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

---

### Task 1: Dry-run config, webhook branch, response helper, docs

**Files:**
- Modify: `hypertrade/config.py` (add `dry_run` field after the idempotency settings, ~line 109)
- Modify: `hypertrade/routes/webhooks.py` (move client construction below a new dry-run branch ~line 169-202; add `_build_dry_run_response` after `_build_response` ~line 527)
- Modify: `.env.example`
- Modify: `README.md`
- Test: `tests/test_webhook.py`

**Interfaces:**
- Produces: `Settings.dry_run: bool` (default `False`).
- Produces: `hypertrade.routes.webhooks._build_dry_run_response(payload: TradingViewWebhook, *, signal: SignalType, side: Side, symbol: str, order_request: OrderRequest) -> dict` — returns the `dry_run` response dict.
- Consumes: existing `make_app`, `StubHyperliquidService`, `BASE_PAYLOAD` from `tests/test_webhook.py`.

- [ ] **Step 1: Write the failing test** — append to `tests/test_webhook.py`:

```python
# ===================================================================
# Dry-Run (Demo) Mode
# ===================================================================

def test_webhook_dry_run_returns_dry_run_without_placing(monkeypatch, tmp_path):
    """HYPERTRADE_DRY_RUN=true: webhook validated, but no order/DB/idempotency."""
    monkeypatch.setenv("HYPERTRADE_DRY_RUN", "true")
    monkeypatch.setenv("HYPERTRADE_DB_PATH", str(tmp_path / "dryrun.db"))
    StubHyperliquidService.reset()
    app = make_app(monkeypatch, secret="secret")
    client = TestClient(app)

    payload = copy.deepcopy(BASE_PAYLOAD)
    resp = client.post("/webhook", json=payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "dry_run"
    assert data["signal"] == "CLOSE_SHORT"
    assert data["side"] == "buy"
    assert data["symbol"] == "SOL"
    assert data["contracts"] == "46231.75300000"
    assert data["price"] == "183.81"

    # Nothing left the process: no exchange call, no DB row.
    assert StubHyperliquidService.call_count == 0
    assert app.state.db.get_orders(limit=10) == []
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3.11 -m pytest tests/test_webhook.py -k dry_run -p no:warnings -q`
Expected: FAIL — without the feature the order is placed (`status == "ok"`, `call_count == 1`), so the assertions on `"dry_run"` and `call_count == 0` fail.

- [ ] **Step 3: Add the `dry_run` setting** — in `hypertrade/config.py`, immediately after the idempotency settings block (the line `idempotency_inflight_timeout: int = 60  # seconds before an in_progress reservation is reclaimable`), add:

```python

    # Dry-run / demo: accept and fully validate webhooks but never place orders,
    # write to the DB, touch the idempotency store, or send Telegram messages.
    dry_run: bool = False
```

- [ ] **Step 4: Add the dry-run response helper** — in `hypertrade/routes/webhooks.py`, immediately after the `_build_response` function (ends ~line 527, with `"received_at": datetime.now(timezone.utc).isoformat(),` then `}`), add:

```python

def _build_dry_run_response(
    payload: TradingViewWebhook,
    *,
    signal: SignalType,
    side: Side,
    symbol: str,
    order_request: OrderRequest,
) -> dict:
    """Mirror of `_build_response` for dry-run: echo the order that *would* be sent."""
    return {
        "status": "dry_run",
        "signal": signal.value,
        "side": side.value,
        "symbol": symbol,
        "ticker": payload.general.ticker,
        "action": payload.order.action,
        "contracts": str(order_request.qty),
        "price": str(order_request.price),
        "leverage": order_request.leverage,
        "reduce_only": order_request.reduce_only,
        "subaccount": order_request.subaccount,
        "received_at": datetime.now(timezone.utc).isoformat(),
    }
```

(`OrderRequest`, `SignalType`, `Side`, `datetime`, `timezone` are already imported in this module.)

- [ ] **Step 5: Branch into dry-run before constructing the client** — in `hypertrade/routes/webhooks.py`, first DELETE the existing client-construction block (currently ~lines 169-174):

```python
    client = HyperliquidService(
        base_url=settings.api_url,
        master_addr=settings.master_addr,
        api_wallet_priv=settings.api_wallet_priv.get_secret_value(),
        subaccount_addr=vault_address,
    )
```

Then, immediately AFTER the `log.debug("Order request prepared: ...")` call (the block that ends with `order_request.reduce_only,\n    )`, ~line 202) and BEFORE the `# ===... EXECUTION ...` comment, INSERT the branch followed by the re-located client construction:

```python

    # Dry-run / demo: the full pipeline above is exercised, but nothing leaves
    # the process — no exchange call, no DB write, no idempotency, no Telegram.
    if settings.dry_run:
        log.info(
            "DRY-RUN: order NOT placed | %s %s qty=%s price=%s lev=%sx reduce_only=%s",
            symbol,
            side.value,
            order_request.qty,
            order_request.price,
            order_request.leverage or 1,
            order_request.reduce_only,
        )
        return _build_dry_run_response(
            payload, signal=signal, side=side, symbol=symbol, order_request=order_request
        )

    client = HyperliquidService(
        base_url=settings.api_url,
        master_addr=settings.master_addr,
        api_wallet_priv=settings.api_wallet_priv.get_secret_value(),
        subaccount_addr=vault_address,
    )
```

The branch returns before the idempotency reserve and execution blocks, so none of those run in dry-run. The `client` is only used later by `_place_order_with_retry(client, ...)`, so relocating its construction below the branch is safe.

- [ ] **Step 6: Run the dry-run test to verify it passes**

Run: `python3.11 -m pytest tests/test_webhook.py -k dry_run -p no:warnings -q`
Expected: PASS (1 test).

- [ ] **Step 7: Run the full suite (no regressions)**

Run: `python3.11 -m pytest -p no:warnings -q`
Expected: PASS — all existing webhook/idempotency/history tests still green (the default `dry_run=False` path is unchanged).

- [ ] **Step 8: Document the switch** — in `.env.example`, after the `HYPERTRADE_LOG_LEVEL=INFO` line, add:

```dotenv

# Dry-run / demo mode: validate incoming webhooks but place NO orders
# (no Hyperliquid call, no DB writes, no Telegram). Useful to test wiring.
HYPERTRADE_DRY_RUN=false
```

Then in `README.md`, add a bullet to the `## Features` list (after the leverage bullet):

```markdown
- Dry-run / demo mode (`HYPERTRADE_DRY_RUN=true`): validate webhooks without trading.
```

- [ ] **Step 9: Commit**

```bash
git add hypertrade/config.py hypertrade/routes/webhooks.py .env.example README.md tests/test_webhook.py
git commit -m "$(cat <<'EOF'
feat(webhook): add HYPERTRADE_DRY_RUN demo mode

Validate and signal-map webhooks without any side effect: no
Hyperliquid order, no DB write, no idempotency reservation, no
Telegram. Returns a "dry_run" response echoing the order that
would have been placed.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Startup WARNING banner for dry-run

**Files:**
- Modify: `hypertrade/daemon.py` (in `create_daemon`, after `log_endpoints(app)` and before `return app`)
- Test: `tests/test_webhook.py`

**Interfaces:**
- Consumes: `Settings.dry_run` (from Task 1), `make_app` (test harness).

- [ ] **Step 1: Write the failing test** — append to `tests/test_webhook.py`:

```python
def test_dry_run_logs_startup_warning(monkeypatch, caplog):
    """Enabling dry-run logs a WARNING banner so a demo daemon isn't mistaken for live."""
    import logging as _logging
    monkeypatch.setenv("HYPERTRADE_DRY_RUN", "true")
    with caplog.at_level(_logging.WARNING, logger="uvicorn.error"):
        make_app(monkeypatch, secret="secret")
    assert any("DRY-RUN MODE" in r.getMessage() for r in caplog.records)
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3.11 -m pytest tests/test_webhook.py -k dry_run_logs_startup_warning -p no:warnings -q`
Expected: FAIL — no "DRY-RUN MODE" record is emitted yet.

- [ ] **Step 3: Emit the banner** — in `hypertrade/daemon.py`, inside `create_daemon`, replace the final `return app` (after the `log_endpoints(app)` call) with:

```python
    if settings.dry_run:
        log.warning(
            "⚠️  DRY-RUN MODE ENABLED — webhooks are validated but NO orders are "
            "sent to Hyperliquid, NO DB writes, NO Telegram notifications."
        )

    return app
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python3.11 -m pytest tests/test_webhook.py -k dry_run_logs_startup_warning -p no:warnings -q`
Expected: PASS (1 test).

- [ ] **Step 5: Run the full suite (no regressions)**

Run: `python3.11 -m pytest -p no:warnings -q`
Expected: PASS (all tests).

- [ ] **Step 6: Commit**

```bash
git add hypertrade/daemon.py tests/test_webhook.py
git commit -m "$(cat <<'EOF'
feat(daemon): warn at startup when dry-run mode is enabled

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Self-Review

**Spec coverage:**
- §3.1 config `dry_run: bool = False` → Task 1, Step 3. ✅
- §3.2 injection point after `OrderRequest`, before client/idempotency/execution → Task 1, Step 5. ✅
- §3.2 no-action path unaffected → no change made to that branch (verified: branch added after it). ✅
- §3.3 `_build_dry_run_response` with the exact field set → Task 1, Step 4. ✅
- §3.4 startup WARNING banner → Task 2, Step 3. ✅
- §4 behavior (dry_run → 200 `dry_run`, no side effects) → Task 1, Steps 1 & 5. ✅
- §5 tests (status `dry_run`, `place_order` never called, no DB rows) → Task 1, Step 1; banner test → Task 2, Step 1. ✅
- §6 docs (`.env.example`, README) → Task 1, Step 8. ✅

**Placeholder scan:** No TBD/TODO/"similar to". Every code step shows literal code; Step 5 names the exact block to delete and the exact insertion point.

**Type consistency:** `dry_run` (bool) defined in Task 1 Step 3, read in Task 1 Step 5 (`settings.dry_run`) and Task 2 Step 3 (`settings.dry_run`). `_build_dry_run_response(payload, *, signal, side, symbol, order_request) -> dict` defined in Step 4 and called identically in Step 5. Response `"status": "dry_run"` matches the test assertion in Task 1 Step 1.

**Note on test isolation:** Task 1's test sets `HYPERTRADE_DB_PATH` to a `tmp_path` DB so `get_orders` starts empty; both new tests set `HYPERTRADE_DRY_RUN` via `monkeypatch.setenv` before `make_app`, which calls `get_settings.cache_clear()` so the flag is picked up.
