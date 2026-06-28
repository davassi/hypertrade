# Order Failure Logging Enrichment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When an order fails, emit enough diagnostic context in the logs — correlation IDs (`req_id`/`cloid`), full order context, the price actually sent, and the exchange's full response — to reconstruct the root cause.

**Architecture:** Add one shared `format_log_context()` helper in `hypertrade/logging.py`, carry `req_id` on the request-scoped `OrderRequest` dataclass (it already carries `cloid`), and enrich the existing failure log lines across the three execution layers (execution client → service → webhook). No structural refactor, no DB migration, no behaviour change.

**Tech Stack:** Python 3.11 (test runner), FastAPI, hyperliquid-python-sdk, pytest, `unittest.mock`.

## Global Constraints

- Run tests with `python3.11 -m pytest` (the repo's `python3.14` has no pytest).
- This stays a **thin executor**: observability only — no trading/position logic.
- **Log stream only.** No change to the `failures`/`orders` DB schema, the `HyperliquidError` taxonomy, retry counts, or pricing logic.
- **No secrets in failure logs.** Only order parameters, exchange responses, and computed prices may be logged. The API private key and `general.secret` must never appear. The pre-existing `log.debug("Full webhook payload: %s", raw)` is out of scope and unchanged.
- Failure context is logged at the **same level as the failure** (ERROR for terminal/exchange rejects, WARNING for retryable attempts), so it survives the production INFO daemon level.
- App loggers use `logging.getLogger("uvicorn.error")`.

---

### Task 1: Shared `format_log_context` helper

**Files:**
- Modify: `hypertrade/logging.py` (append a function near the top, after the `log = ...` line)
- Test: `tests/test_logging_context.py` (create)

**Interfaces:**
- Produces: `format_log_context(**fields: object) -> str` — joins `key=value` pairs with single spaces, skipping any field whose value is `None`; never raises. Returns `""` when all fields are `None`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_logging_context.py`:

```python
"""Tests for format_log_context — the shared diagnostic-context formatter used to
build a uniform correlation suffix on failure log lines."""

from __future__ import annotations

import pathlib
import sys

REPO_ROOT = str(pathlib.Path(__file__).resolve().parents[1])
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from hypertrade.logging import format_log_context


def test_joins_key_values_in_order():
    assert format_log_context(symbol="SOL", side="buy", size=2.0) == "symbol=SOL side=buy size=2.0"


def test_skips_none_values():
    assert format_log_context(symbol="SOL", cloid=None, req_id="r-1") == "symbol=SOL req_id=r-1"


def test_empty_when_all_none():
    assert format_log_context(a=None, b=None) == ""


def test_tolerates_arbitrary_reprable_objects():
    assert format_log_context(payload={"k": 1}) == "payload={'k': 1}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3.11 -m pytest tests/test_logging_context.py -v`
Expected: FAIL with `ImportError: cannot import name 'format_log_context'`.

- [ ] **Step 3: Write minimal implementation**

In `hypertrade/logging.py`, immediately after the `log = pylog.getLogger("uvicorn.error")` line, add:

```python
def format_log_context(**fields: object) -> str:
    """Format diagnostic fields as a space-separated ``key=value`` string.

    Used to build a uniform correlation/context suffix for failure log lines so
    the prefix is defined once (no copy-paste across call sites). Skips fields
    whose value is ``None``; never raises.
    """
    return " ".join(f"{key}={value}" for key, value in fields.items() if value is not None)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3.11 -m pytest tests/test_logging_context.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add hypertrade/logging.py tests/test_logging_context.py
git commit -m "feat(logging): add format_log_context helper for failure-log correlation"
```

---

### Task 2: Carry `req_id` on `OrderRequest`

**Files:**
- Modify: `hypertrade/routes/hyperliquid_service.py` (the `OrderRequest` dataclass, ~lines 23-40)
- Modify: `hypertrade/routes/webhooks.py` (the `OrderRequest(...)` construction in `hypertrade_webhook`, ~lines 255-267)
- Test: `tests/test_webhook.py` (add one test)

**Interfaces:**
- Consumes: nothing new.
- Produces: `OrderRequest.req_id: Optional[str] = None` — the request-scoped correlation id, set by the webhook handler. Consumed by Tasks 4 and 5.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_webhook.py`:

```python
def test_order_request_carries_req_id(monkeypatch):
    """The webhook must thread its request id onto OrderRequest so downstream
    failure logs can correlate back to the originating request."""
    StubHyperliquidService.reset()
    app = make_app(monkeypatch, secret="secret")
    client = TestClient(app)

    resp = client.post("/webhook", json=copy.deepcopy(BASE_PAYLOAD))
    assert resp.status_code == 200, resp.text
    order_req = StubHyperliquidService.last_order_request
    assert order_req is not None
    assert isinstance(order_req.req_id, str) and order_req.req_id
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3.11 -m pytest tests/test_webhook.py::test_order_request_carries_req_id -v`
Expected: FAIL with `AttributeError: 'OrderRequest' object has no attribute 'req_id'`.

- [ ] **Step 3: Write minimal implementation**

In `hypertrade/routes/hyperliquid_service.py`, add the field at the end of the `OrderRequest` dataclass (after the `cloid` field):

```python
    # Request-scoped correlation id (the webhook's req_id). Threaded onto the
    # request so failure logs in the service/webhook layers can be tied back to
    # the originating request. Distinct from cloid (the exchange-side order id).
    req_id: Optional[str] = None
```

In `hypertrade/routes/webhooks.py`, in the `OrderRequest(...)` construction inside `hypertrade_webhook`, add the `req_id` argument (the local `req_id` is already computed just above, where `cloid_seed` is derived):

```python
    order_request = OrderRequest(
        symbol=symbol,
        side=side,
        signal=signal,
        qty=contracts,
        price=price,
        reduce_only=reduce_only,
        post_only=False,
        client_id=None,
        leverage=leverage,
        subaccount=vault_address,
        cloid=cloid,
        req_id=req_id,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3.11 -m pytest tests/test_webhook.py::test_order_request_carries_req_id -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add hypertrade/routes/hyperliquid_service.py hypertrade/routes/webhooks.py tests/test_webhook.py
git commit -m "feat(exec): carry req_id on OrderRequest for failure-log correlation"
```

---

### Task 3: Log the submitted pricing in the execution client

**Files:**
- Modify: `hypertrade/routes/hyperliquid_execution_client.py` (add import; `limit_order` ~lines 99-123; `market_order` ~lines 125-157)
- Test: `tests/test_execution_client_price.py` (add one test)

**Interfaces:**
- Consumes: `format_log_context` from Task 1.
- Produces: one INFO log line per submit carrying the exact price about to be sent — so a subsequent reject has the price on the immediately preceding line.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_execution_client_price.py`:

```python
def test_market_order_logs_submitted_pricing(monkeypatch, caplog):
    """Before submitting a MARKET IOC, the client logs the mid and the exact
    aggressive/normalized price it is about to send, tagged with the cloid — the
    primary diagnostic for a 'bad price / could not match' reject."""
    import logging as _logging
    client = _client(monkeypatch, 3)
    client.data.get_mid = MagicMock(return_value=1000.0)

    with caplog.at_level(_logging.INFO, logger="uvicorn.error"):
        client.market_order(
            symbol="SOL", side=PositionSide.LONG, size=2.0,
            premium_bps=500, cloid="0x" + "a" * 32,
        )

    msgs = [r.getMessage() for r in caplog.records]
    assert any(
        "mid=1000" in m and "norm_px=1050" in m and ("0x" + "a" * 32) in m
        for m in msgs
    ), msgs
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3.11 -m pytest tests/test_execution_client_price.py::test_market_order_logs_submitted_pricing -v`
Expected: FAIL (no log record matches — the pricing line does not exist yet).

- [ ] **Step 3: Write minimal implementation**

In `hypertrade/routes/hyperliquid_execution_client.py`, add the import next to the other `from .` / `from hypertrade` imports (after line 14, `from .hyperliquid_errors import translate_request_errors`):

```python
from hypertrade.logging import format_log_context
```

In `market_order`, insert the log line immediately before the `with translate_request_errors("market_order"):` block (after `norm_px = self._normalize_price(...)`):

```python
        log.info(
            "Submitting MARKET IOC | %s",
            format_log_context(
                symbol=symbol, side=side.value, size=size,
                mid=f"{mid:.6f}", slippage_bps=slippage_bps,
                aggressive_px=f"{aggressive_px:.6f}", norm_px=norm_px,
                reduce_only=reduce_only, cloid=cloid,
            ),
        )
```

In `limit_order`, insert the log line immediately before the `with translate_request_errors("limit_order"):` block (after `norm_price = self._normalize_price(...)`):

```python
        log.info(
            "Submitting LIMIT %s | %s",
            tif,
            format_log_context(
                symbol=symbol, side=side.value, size=size,
                price=price, norm_price=norm_price,
                reduce_only=reduce_only, cloid=cloid,
            ),
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3.11 -m pytest tests/test_execution_client_price.py -v`
Expected: PASS (all price tests, including the new one).

- [ ] **Step 5: Commit**

```bash
git add hypertrade/routes/hyperliquid_execution_client.py tests/test_execution_client_price.py
git commit -m "feat(exec): log submitted price (mid/aggressive/norm) before each order"
```

---

### Task 4: Enrich the service-layer failure logs

**Files:**
- Modify: `hypertrade/routes/hyperliquid_service.py` (add import + `_safe_json` helper; `place_order` failure branches — leverage reject ~lines 159-164, no-response ~216-218, unexpected-shape ~222-224, exchange-reject ~230-238, unexpected-status ~239-241)
- Test: `tests/test_hyperliquid_service.py` (add two tests)

**Interfaces:**
- Consumes: `format_log_context` (Task 1), `OrderRequest.req_id` (Task 2).
- Produces: enriched ERROR logs carrying order context + the full exchange payload at each failure site.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_hyperliquid_service.py`:

```python
def test_exchange_reject_logs_full_context(monkeypatch, caplog):
    """On an exchange reject the ERROR log must carry the order context (symbol,
    side, size, cloid, req_id) and the error/payload, not just the error string."""
    import logging as _logging
    svc, client = _service(monkeypatch)
    client.market_order.return_value = {
        "response": {"data": {"statuses": [{"error": "Order has invalid price."}]}}
    }
    with caplog.at_level(_logging.ERROR, logger="uvicorn.error"):
        with pytest.raises(HyperliquidRejection):
            svc.place_order(OrderRequest(
                symbol="SOL", side=Side.BUY, signal=SignalType.OPEN_LONG,
                qty=Decimal("2"), price=Decimal("100"), leverage=3,
                cloid="0x" + "a" * 32, req_id="req-123",
            ))
    msg = " ".join(r.getMessage() for r in caplog.records)
    assert "symbol=SOL" in msg
    assert "side=buy" in msg
    assert "size=2" in msg
    assert ("cloid=0x" + "a" * 32) in msg
    assert "req_id=req-123" in msg
    assert "Order has invalid price." in msg


def test_no_response_logs_context(monkeypatch, caplog):
    """A None exchange response is logged with the order context before raising."""
    import logging as _logging
    svc, client = _service(monkeypatch)
    client.market_order.return_value = None
    with caplog.at_level(_logging.ERROR, logger="uvicorn.error"):
        with pytest.raises(HyperliquidAPIError):
            svc.place_order(OrderRequest(
                symbol="SOL", side=Side.BUY, signal=SignalType.OPEN_LONG,
                qty=Decimal("1"), price=Decimal("100"), leverage=1,
                cloid="0x" + "b" * 32, req_id="req-456",
            ))
    msg = " ".join(r.getMessage() for r in caplog.records)
    assert "symbol=SOL" in msg and "req_id=req-456" in msg and ("cloid=0x" + "b" * 32) in msg
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3.11 -m pytest tests/test_hyperliquid_service.py::test_exchange_reject_logs_full_context tests/test_hyperliquid_service.py::test_no_response_logs_context -v`
Expected: FAIL (the current logs carry only `symbol` + the error/`res`, so the `cloid=`/`req_id=`/`side=` assertions fail).

- [ ] **Step 3: Write minimal implementation**

In `hypertrade/routes/hyperliquid_service.py`, add the import after the existing `from .hyperliquid_errors import (...)` block:

```python
from hypertrade.logging import format_log_context
```

Add this module-level helper just after the `log = logging.getLogger("uvicorn.error")` line:

```python
def _safe_json(obj: object) -> str:
    """json.dumps that never raises — falls back to repr for non-serialisable payloads.

    A logging path must never mask the original failure with a serialisation error.
    """
    try:
        return json.dumps(obj)
    except (TypeError, ValueError):
        return repr(obj)
```

Replace the **leverage-reject** log line (inside the `if leverage_response.get('status') != 'ok':` block):

```python
                log.error(
                    "Leverage update REJECTED | %s | response=%s",
                    format_log_context(
                        symbol=symbol, requested_leverage=leverage,
                        max_leverage=max_leverage, is_cross=is_cross,
                        cloid=request.cloid, req_id=request.req_id,
                    ),
                    _safe_json(leverage_response),
                )
```

Replace the **no-response** branch (`if res is None:`):

```python
        if res is None:
            log.error(
                "Order execution failed (no response from API) | %s",
                format_log_context(
                    symbol=symbol, side=request.side.value, size=size,
                    reduce_only=request.reduce_only,
                    cloid=request.cloid, req_id=request.req_id,
                ),
            )
            raise HyperliquidAPIError("Order Creation did not work")
```

Replace the **unexpected-shape** `except` log line:

```python
            except (KeyError, TypeError, IndexError) as exc:
                log.error(
                    "Unexpected order response shape | %s | response=%s",
                    format_log_context(
                        symbol=symbol, side=request.side.value, size=size,
                        cloid=request.cloid, req_id=request.req_id,
                    ),
                    _safe_json(res),
                )
                raise HyperliquidAPIError(f"Unexpected order response shape: {res}") from exc
```

Replace the **exchange-reject** branch (`elif "error" in status:`) log line (keep the `raise HyperliquidRejection(...)` unchanged):

```python
            elif "error" in status:
                log.error(
                    "Order rejected by exchange | %s | error=%s | response=%s",
                    format_log_context(
                        symbol=symbol, side=request.side.value, size=size,
                        reduce_only=request.reduce_only, leverage=leverage,
                        cloid=request.cloid, req_id=request.req_id,
                    ),
                    status["error"],
                    _safe_json(res),
                )
                raise HyperliquidRejection(f"Exchange rejected order: {status['error']}")
```

Replace the **unexpected-status** `else` log line:

```python
            else:
                log.error(
                    "Unexpected order status | %s | status=%s",
                    format_log_context(
                        symbol=symbol, side=request.side.value, size=size,
                        cloid=request.cloid, req_id=request.req_id,
                    ),
                    _safe_json(status),
                )
                raise HyperliquidAPIError(f"Unexpected order status: {status}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3.11 -m pytest tests/test_hyperliquid_service.py -v`
Expected: PASS (all service tests, including the two new ones — the existing reject/shape tests still pass since exceptions are unchanged).

- [ ] **Step 5: Commit**

```bash
git add hypertrade/routes/hyperliquid_service.py tests/test_hyperliquid_service.py
git commit -m "feat(exec): enrich service failure logs with order context + payload"
```

---

### Task 5: Enrich the webhook retry-loop and handler logs

**Files:**
- Modify: `hypertrade/routes/webhooks.py` (add import; `_place_order_with_retry` ~lines 85-133; the three exception handlers in `hypertrade_webhook` ~lines 330-401)
- Test: `tests/test_webhook.py` (add one test)

**Interfaces:**
- Consumes: `format_log_context` (Task 1), `OrderRequest.req_id` (Task 2).
- Produces: the terminal network/API retry line now includes `str(e)` + correlation; rejection/validation lines and the three handlers include `req_id`/`cloid`/`symbol`/`side`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_webhook.py`:

```python
def test_network_failure_logs_error_and_correlation(monkeypatch, caplog):
    """The terminal retry line must carry the underlying error string AND
    correlation (cloid); the handler line must carry the req_id."""
    import logging as _logging
    StubHyperliquidService.reset()
    StubHyperliquidService.should_fail = True
    StubHyperliquidService.failure_type = "network"
    app = make_app(monkeypatch, secret="secret")
    client = TestClient(app)

    with caplog.at_level(_logging.WARNING, logger="uvicorn.error"):
        resp = client.post("/webhook", json=copy.deepcopy(BASE_PAYLOAD))
    assert resp.status_code == 503

    msgs = [r.getMessage() for r in caplog.records]
    assert any(
        "Order placement failed after" in m and "Network timeout" in m and "cloid=" in m
        for m in msgs
    ), msgs
    assert any(
        "Network error placing order" in m and "req_id=" in m for m in msgs
    ), msgs
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3.11 -m pytest tests/test_webhook.py::test_network_failure_logs_error_and_correlation -v`
Expected: FAIL (the terminal line currently logs only the attempt count, with no `str(e)`/`cloid`, and the handler line has no `req_id`).

- [ ] **Step 3: Write minimal implementation**

In `hypertrade/routes/webhooks.py`, add the import next to the other relative imports (e.g. after `from ..idempotency import ReserveOutcome`):

```python
from ..logging import format_log_context
```

In `_place_order_with_retry`, build a correlation context once, right after `cloid = order_request.cloid`:

```python
    cloid = order_request.cloid
    ctx = format_log_context(
        symbol=order_request.symbol,
        side=getattr(order_request.side, "value", order_request.side),
        cloid=cloid,
        req_id=order_request.req_id,
    )
```

Then replace the four log lines inside the `try/except` of the retry loop:

```python
        except HyperliquidRejection as e:
            if attempt < 1:
                log.warning("Order REJECTED (attempt %d) — retrying once with a fresh price: %s | %s", attempt + 1, str(e), ctx)
                continue
            log.error("Order REJECTED again after one retry — surfacing terminal (no further retry): %s | %s", str(e), ctx)
            raise
        except HyperliquidValidationError as e:
            # Don't retry validation errors - they're permanent
            log.warning("Order validation failed, not retrying: %s | %s | order=%s", str(e), ctx, order_request)
            raise
        except (HyperliquidNetworkError, HyperliquidAPIError) as e:
            if attempt < max_retries:
                wait_time = 2 ** attempt  # Exponential backoff: 1s, 2s, 4s...
                log.warning(
                    "Order placement attempt %d/%d failed, retrying in %ds: %s | %s",
                    attempt + 1, max_retries + 1, wait_time, str(e), ctx
                )
                await asyncio.sleep(wait_time)
            else:
                log.error("Order placement failed after %d attempts: %s | %s", max_retries + 1, str(e), ctx)
                raise
```

In `hypertrade_webhook`, enrich the three exception-handler log lines (leave the `db.log_order`/`db.log_failure`/`raise HTTPException` parts unchanged). The locals `req_id`, `cloid`, `symbol`, `side` are all in scope at these points:

```python
    except HyperliquidValidationError as e:
        log.warning(
            "Order validation error: %s | %s", e,
            format_log_context(req_id=req_id, cloid=cloid, symbol=symbol, side=side.value),
        )
```

```python
    except HyperliquidNetworkError as e:
        log.error(
            "Network error placing order (after retries): %s | %s", e,
            format_log_context(req_id=req_id, cloid=cloid, symbol=symbol, side=side.value),
        )
```

```python
    except HyperliquidAPIError as e:
        log.error(
            "API error placing order (after retries): %s | %s", e,
            format_log_context(req_id=req_id, cloid=cloid, symbol=symbol, side=side.value),
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3.11 -m pytest tests/test_webhook.py -v`
Expected: PASS (all webhook tests, including the new one; the existing 503/502/400 retry tests still pass — exceptions/status codes are unchanged).

- [ ] **Step 5: Commit**

```bash
git add hypertrade/routes/webhooks.py tests/test_webhook.py
git commit -m "feat(exec): enrich retry-loop + handler logs with error + correlation"
```

---

### Task 6: Secret-safety regression test

**Files:**
- Test: `tests/test_webhook.py` (add one test)

**Interfaces:**
- Consumes: the enriched failure logs from Tasks 4-5.
- Produces: a guarantee that failure-level logs never emit the webhook secret.

- [ ] **Step 1: Write the test (expected to pass immediately — it guards the constraint)**

Append to `tests/test_webhook.py`:

```python
def test_failure_logs_do_not_leak_secret(monkeypatch, caplog):
    """A failing order must not emit the webhook secret in failure-level (WARNING+)
    logs. (The DEBUG full-payload log is out of scope and excluded by the level.)"""
    import logging as _logging
    StubHyperliquidService.reset()
    StubHyperliquidService.should_fail = True
    StubHyperliquidService.failure_type = "api"
    app = make_app(monkeypatch, secret="topsecret-xyz")
    client = TestClient(app)

    payload = copy.deepcopy(BASE_PAYLOAD)
    payload["general"]["secret"] = "topsecret-xyz"

    with caplog.at_level(_logging.WARNING, logger="uvicorn.error"):
        resp = client.post("/webhook", json=payload)
    assert resp.status_code == 502

    for record in caplog.records:
        assert "topsecret-xyz" not in record.getMessage()
```

- [ ] **Step 2: Run test to verify it passes**

Run: `python3.11 -m pytest tests/test_webhook.py::test_failure_logs_do_not_leak_secret -v`
Expected: PASS (our failure logs carry only order context + exchange responses, never the secret).

- [ ] **Step 3: Run the full suite**

Run: `python3.11 -m pytest -q`
Expected: PASS (entire suite green — the suite is hermetic per project memory).

- [ ] **Step 4: Commit**

```bash
git add tests/test_webhook.py
git commit -m "test(exec): assert failure logs never leak the webhook secret"
```

---

## Self-Review

**Spec coverage:**
- Gap 1 (no `req_id`/`cloid` correlation) → Tasks 2, 4, 5.
- Gap 2 (exchange reject drops order context) → Task 4 (exchange-reject branch).
- Gap 3 (computed aggressive price never logged) → Task 3.
- Gap 4 (terminal retry line drops the error) → Task 5 (terminal network/API line now includes `str(e)`).
- Constraint "no secrets in logs" → Task 6.
- Constraint "log stream only / no schema change / no taxonomy change" → no task touches DB schema or `hyperliquid_errors.py`; all `raise`/status codes preserved.
- Shared helper / no duplicated logic → Task 1 (`format_log_context`), used by Tasks 3-5.

**Placeholder scan:** No TBD/TODO; every code step shows the full code.

**Type consistency:** `format_log_context(**fields) -> str` defined in Task 1 and called with keyword args only in Tasks 3-5. `OrderRequest.req_id: Optional[str]` defined in Task 2 and read as `request.req_id` / `order_request.req_id` in Tasks 4-5. `_safe_json(obj) -> str` defined and used only within Task 4's file. `side.value` is used where `side`/`request.side` is a `Side`/`PositionSide` enum (all such call sites); the retry-loop uses `getattr(..., "value", ...)` to tolerate the enum.
