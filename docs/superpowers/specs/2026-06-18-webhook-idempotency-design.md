# Webhook Idempotency via Nonce — Design

- **Date:** 2026-06-18
- **Status:** Approved (pending spec review)
- **Component:** `hypertrade` webhook → Hyperliquid order placement

## 1. Problem

The `/webhook` endpoint places a real order on every accepted request. There is
**no replay or duplicate protection**:

- A duplicate delivery (sender retry on timeout, double-fire) places a **second
  order** — real money.
- A replayed signed request (same valid `general.secret`) is accepted again.

The order DB has `orders.request_id TEXT UNIQUE`, but `request_id` is a
server-generated `uuid4()` per HTTP request (`middleware/logging.py`), so it does
**not** dedupe a re-delivered logical signal. `order.id` is a strategy order name
(e.g. `"Long Entry"`) that repeats for every signal of its type, so it is not a
usable key either.

The signal source is a controlled internal project (`../volume-test`,
`src/main/order-intent.js`), **not** TradingView, so the sender can attach a
unique, stable **nonce** to each leg-order. One pair trade = two leg payloads, so
idempotency is **per webhook (per leg-order)**.

## 2. Goals / Non-goals

**Goals**
- Guarantee at-most-once order placement per nonce.
- A failed placement is safely **retryable** with the same nonce (consume on
  success only).
- Atomic against concurrent double-fire of the same nonce.
- Survive process restarts (persistent store).
- Gated by a startup settings flag.

**Non-goals**
- Implementing the sender's nonce generation (`volume-test` — separate project).
- Cryptographic replay-freshness windows (timestamp/nonce signing). The shared
  `general.secret` plus per-order nonce dedup is sufficient for the internal
  threat model.
- Periodic pruning of old `completed` nonces (deferred; see §11).

## 3. Configuration

| Setting | Default | Effect |
| --- | --- | --- |
| `HYPERTRADE_IDEMPOTENCY_ENABLED` | **`true`** | When true, idempotency is enforced (see below). When false, behavior is identical to today and any `nonce` is ignored. Read once at startup. |
| `HYPERTRADE_IDEMPOTENCY_INFLIGHT_TIMEOUT` | `60` (seconds) | An `in_progress` reservation older than this is considered stale and reclaimable, so a crash between reserve and complete cannot block a nonce forever. |

When `HYPERTRADE_IDEMPOTENCY_ENABLED` is true:
- `general.nonce` is **required**; a request without it is rejected `400`.
- The order DB is **required**. If `db_enabled` is false, the daemon **fails
  fast at startup** (config `model_validator` raises) — there is no store to
  dedupe against otherwise.

> **Breaking change (default-on):** senders that do not provide `general.nonce`
> will receive `400`. The documented TradingView template does not send a nonce;
> README and `docs/tradingview-webhook.md` are updated as part of this work
> (§9). The effective contract becomes: *every order-placing request must carry a
> unique, retry-stable `general.nonce`.*

## 4. Nonce contract

- Field: `general.nonce` — an opaque string. `general` already allows
  `additionalProperties: true`, so adding it is non-breaking to the schema.
- The **sender** is responsible for:
  - generating a nonce that is **unique per leg-order**, and
  - reusing the **same** nonce when retrying that same leg-order.
- The server treats the nonce as opaque; it never parses or derives meaning from
  it.
- Schema: add `nonce` to the `general` properties as an optional string
  (`minLength: 1`). Presence is enforced in the handler (not the schema) so the
  rule can depend on the runtime flag.

## 5. Data model

A dedicated table, isolated from the analytics `orders`/`failures` tables:

```sql
CREATE TABLE IF NOT EXISTS idempotency_keys (
    nonce        TEXT PRIMARY KEY,
    status       TEXT NOT NULL,   -- 'in_progress' | 'completed'
    request_id   TEXT,
    result_json  TEXT,            -- JSON response to replay on a completed duplicate
    created_at   TEXT NOT NULL,   -- ISO8601 UTC; used for stale-reclaim
    completed_at TEXT
);
```

## 6. Component: `IdempotencyStore` (`hypertrade/idempotency.py`)

A small, independently testable unit wrapping the table. It depends only on the
SQLite connection (injected), not on FastAPI.

```python
class ReserveOutcome(Enum):
    NEW = "new"                       # reserved; caller must place the order
    DUPLICATE_COMPLETED = "completed" # already done; replay stored result
    IN_FLIGHT = "in_flight"           # another reservation active and not stale

@dataclass
class ReserveResult:
    outcome: ReserveOutcome
    result: Optional[dict] = None     # set when DUPLICATE_COMPLETED

class IdempotencyStore:
    def reserve(self, nonce: str, request_id: str, inflight_timeout_s: int) -> ReserveResult: ...
    def complete(self, nonce: str, result: dict) -> None: ...
    def release(self, nonce: str) -> None: ...
```

**`reserve` semantics (atomic):**
1. Attempt `INSERT INTO idempotency_keys (nonce, status, request_id, created_at)
   VALUES (?, 'in_progress', ?, <now>)`.
   - Success → `NEW`.
2. On `IntegrityError` (nonce exists), read the existing row:
   - `status == 'completed'` → `DUPLICATE_COMPLETED` with `result =
     json.loads(result_json)`.
   - `status == 'in_progress'` and `created_at` **older** than
     `inflight_timeout_s` → **stale**: reclaim via conditional
     `UPDATE ... SET created_at=<now>, request_id=? WHERE nonce=? AND
     status='in_progress' AND created_at=<old>`; if `rowcount == 1` → `NEW`,
     else another worker won the race → `IN_FLIGHT`.
   - otherwise → `IN_FLIGHT`.

**`complete`:** `UPDATE ... SET status='completed', result_json=?,
completed_at=<now> WHERE nonce=?`.

**`release`:** `DELETE FROM idempotency_keys WHERE nonce=? AND status='in_progress'`
(failure path → frees the nonce for a legitimate retry; the guard avoids deleting
a row another worker already completed).

**Concurrency assumption:** single daemon process (uvicorn). SQLite + the
insert-first pattern serializes the reserve race adequately for this deployment;
documented so a future multi-process deployment revisits it.

## 7. Request flow (handler integration)

Idempotency wraps **only the order-placing path** — a `NO_ACTION` request places
no order, so it is harmless to re-deliver and is not reserved. Nonce **presence**
is still validated early when the flag is on.

```
1. content-type / body / schema / secret            (unchanged)
2. if idempotency_enabled and not general.nonce  -> 400
3. parse_signal -> side
4. if NO_ACTION  -> return "ignored"                 (unchanged; nonce not reserved)
5. if idempotency_enabled:
     r = store.reserve(nonce, request_id, inflight_timeout)
       - DUPLICATE_COMPLETED -> return 200 {"status":"duplicate", **r.result}
       - IN_FLIGHT           -> 409 {"status":"in_flight", "nonce": ...}
       - NEW                 -> continue
6. place order (off the event loop, via asyncio.to_thread)         (existing)
     - success -> store.complete(nonce, response); db.log_order(...); return 200
     - any exception from placement -> store.release(nonce); re-raise
```

The `reserve`/`release`/`complete` calls are no-ops (skipped) when the flag is
off, keeping the disabled path byte-for-byte the current behavior.

## 8. HTTP responses

| Situation | Status | Body |
| --- | --- | --- |
| New nonce, order placed | `200` | existing `_build_response` output |
| Duplicate of a completed nonce | `200` | `{"status": "duplicate", <original result>}` |
| Same nonce in flight (concurrent) | `409` | `{"status": "in_flight", "nonce": "..."}` |
| Missing nonce while enabled | `400` | `{"detail": "general.nonce is required"}` |

Returning `200` on a completed duplicate makes the sender's retry observe success
and stop retrying. `409` signals "retry shortly" for a genuine concurrent
double-fire.

## 9. Documentation updates

- `docs/tradingview-webhook.md`: add `general.nonce` to the field reference and
  to every example payload; document the flag, the `400`-on-missing rule, and the
  DB requirement.
- `README.md`: note `HYPERTRADE_IDEMPOTENCY_ENABLED` (default on),
  `HYPERTRADE_IDEMPOTENCY_INFLIGHT_TIMEOUT`, and the DB-required fail-fast in the
  webhook/security sections.

## 10. Error handling

- DB errors during `reserve`/`complete`/`release` propagate as `503` (consistent
  with the existing network-error mapping) — we must not place or double-place an
  order when the dedup store is unavailable.
- `release` is best-effort within the existing placement `except` blocks; a
  failed release leaves a reclaimable `in_progress` row (covered by the stale
  timeout), never a permanently blocked nonce.

## 11. Testing (TDD)

Unit (`IdempotencyStore`, in-memory sqlite):
- reserve new nonce → `NEW`; row is `in_progress`.
- reserve completed nonce → `DUPLICATE_COMPLETED` with the stored result.
- reserve while `in_progress` (not stale) → `IN_FLIGHT`.
- reserve a **stale** `in_progress` (older than timeout) → reclaimed as `NEW`.
- `complete` then reserve → `DUPLICATE_COMPLETED`.
- `release` then reserve → `NEW` (retryable).

Integration (webhook, via existing `make_app` + `StubHyperliquidService`):
- duplicate completed request → second call places **no** new order (stub
  `call_count` unchanged) and returns `200 "duplicate"`.
- placement failure → nonce released → retry with same nonce places the order.
- missing nonce while enabled → `400`.
- flag disabled → nonce ignored; behavior identical to today (regression guard).
- config: `idempotency_enabled` + `db_enabled=false` → startup raises.

## 12. Out of scope / follow-ups

- `volume-test` sender nonce generation (separate project; this spec defines the
  contract it must satisfy).
- Pruning/retention of old `completed` nonces — unbounded growth is acceptable at
  this volume; revisit with a TTL cleanup if needed.
- Multi-process deployment concurrency (current design assumes single process).

## 13. Self-critique

- **Default-on is breaking** for nonce-less senders and makes the DB mandatory;
  accepted deliberately (the sender is controlled). Mitigated by doc updates and
  startup fail-fast rather than a silent runtime failure.
- **Stale-reclaim is time-based**, so a placement that legitimately takes longer
  than `inflight_timeout` could be reclaimed and re-placed by a concurrent retry.
  The default (60s) is comfortably above the bounded retry budget
  (1+2+4s backoff ≈ 7s plus call latency); documented so the timeout is tuned
  above worst-case placement time.
- **Single-process concurrency assumption** — correct for the current uvicorn
  deployment; flagged for any future scale-out.
