# Order Failure Logging Enrichment — Design

**Date:** 2026-06-28
**Status:** Approved (design)
**Scope:** Diagnostic logging only. No change to the error taxonomy, retry policy, DB schema, or execution behaviour.

## Problem

When an order fails it is currently hard to reconstruct *why* from the logs. Four concrete gaps:

1. **No `req_id` / `cloid` correlation.** The request-logging middleware emits `req_id`, but no log line in the execution path (`hyperliquid_service.place_order`, `webhooks._place_order_with_retry`, the webhook exception handlers) carries it. A failure line cannot be tied back to the originating webhook request nor to the `cloid` the exchange indexed the order under.
2. **Exchange rejections drop the order context.** `hyperliquid_service.py` logs only `symbol` + `status["error"]` on a reject — not the price actually sent, size, side, `reduce_only`, or leverage.
3. **The computed aggressive price is never logged.** `execution_client.market_order` computes `mid → aggressive_px → norm_px` but logs none of it — exactly the value needed to diagnose a "bad price / could not match" reject.
4. **The terminal retry line discards the error.** `webhooks._place_order_with_retry` logs `"Order placement failed after N attempts"` with no `str(e)`.

## Goal

On any order failure, the log stream must carry enough context to diagnose root cause:

- **Correlation:** `req_id` + `cloid` on every failure line in the execution path.
- **Order context:** `symbol`, `side`, `size`, the **price actually sent**, `reduce_only`, `leverage`.
- **Exchange response:** the full response/payload, not just the extracted error string.
- **Pricing:** the computed aggressive pricing (`mid`, slippage bps, `aggressive_px`, `norm_px`) for IOC market orders.

Constraints (from project memory and the approved scope):

- **Log stream only.** No DB/schema migration. `failures` and `log_order` are unchanged.
- **No structural refactor.** No `contextvars`/`logging.Filter` infrastructure, no JSON structured logging.
- **Thin executor.** No new trading/position logic; this is observability only.

## Approach

`OrderRequest` already carries `cloid`. Add a `req_id` field to it, and that request-scoped dataclass becomes the correlation vehicle through all three layers without changing any method signature. `cloid` is the natural correlation key at the execution-client layer (it has no `req_id`); `req_id`+`cloid` apply at the service and webhook layers.

## Components / changes

### 1. Shared context formatter — `hypertrade/logging.py`

A single helper so the `key=value` diagnostic prefix is never copy-pasted across call sites (global rule: no duplicated logic). `hypertrade/logging.py` is a leaf module already imported for logging utilities, so there is no circular-import risk from `execution_client` / `service` / `webhooks`.

```python
def format_log_context(**fields: object) -> str:
    """Format diagnostic fields as a space-separated 'k=v' string, skipping None.

    Used to build a uniform correlation/context prefix on failure log lines.
    Never raises: tolerates None values and arbitrary repr-able objects.
    """
    return " ".join(f"{k}={v}" for k, v in fields.items() if v is not None)
```

### 2. `OrderRequest.req_id`

Add `req_id: Optional[str] = None`. The webhook handler sets it alongside `cloid` when building the `OrderRequest`. Threads `req_id` into the retry loop and `place_order` with no signature churn.

### 3. `execution_client.market_order` / `limit_order`

Emit one **INFO** line immediately before submitting, carrying the pricing that is about to be sent:

- `market_order`: `symbol, side, size, mid, slippage_bps, aggressive_px, norm_px, reduce_only, cloid`.
- `limit_order`: `symbol, side, size, price, norm_price, tif, reduce_only, cloid`.

So when a reject follows, the price actually sent is on the immediately preceding line. INFO level keeps it present under the production INFO daemon level.

### 4. `hyperliquid_service.place_order`

Enrich every failure site with the order context (via `format_log_context`, including `req_id` + `cloid`) and the full payload:

- No-response (`res is None`).
- Unexpected response shape (already logs `res`; add context + correlation).
- **Exchange reject** (`"error" in status`): add `side`, `size`, `reduce_only`, `cloid`, `req_id`, and `json.dumps(res)`.
- Unexpected status.
- Leverage-update reject (already logs the response; add correlation + requested/max leverage context).

### 5. `webhooks._place_order_with_retry`

- Terminal network/API line: include `str(e)` + context (`cloid`, `symbol`, attempt count).
- Rejection lines (retry + terminal): include `cloid`.
- Validation line: already logs `order_request`; keep.

### 6. `webhooks` exception handlers

Add `req_id` + `cloid` + `symbol` + `side` to the three handler log lines (validation / network / API).

## Error handling & safety

- **No secrets in logs.** Only order parameters, exchange responses, and computed prices are logged — the API private key and `general.secret` never appear in those values. The pre-existing `log.debug("Full webhook payload: %s", raw)` (which includes `general.secret` at DEBUG) is out of scope and unchanged.
- **Logging must never raise.** `format_log_context` tolerates `None`; `json.dumps(res)` is wrapped defensively (fall back to `repr(res)` on `TypeError`/`ValueError`) so a non-serialisable payload never masks the original failure.
- **Levels.** Failure context is emitted at the *same level as the failure* (ERROR for terminal/exchange rejects, WARNING for retryable attempts) so it is captured at the production INFO level. The pre-submit pricing line is INFO.

## Testing (TDD)

Write the assertions first, then implement. Tests run with `python3.11 -m pytest`.

- `test_hyperliquid_service.py`: on an exchange-reject response, assert (via `caplog`) the ERROR record contains `cloid`, `symbol`, `side`, `size`, and the response payload. On `res is None` and unexpected-shape, assert context + correlation present.
- `test_hyperliquid_service.py` / new: assert the pre-submit pricing line for a market order contains `mid`, `aggressive_px`, `norm_px`, `cloid`.
- `test_webhook.py`: on a terminal network failure, assert the retry-loop ERROR record contains `str(e)`; on each handler path assert `req_id`/`cloid` present.
- One test asserts that **no secret** (`general.secret` value, private key) appears in captured log output for a failing order.

## Out of scope (YAGNI)

- JSON structured logging.
- A `context` column / migration on the `failures` table.
- `contextvars` / `logging.Filter` correlation infrastructure.
- Any change to the `HyperliquidError` taxonomy, retry counts, or pricing logic.
