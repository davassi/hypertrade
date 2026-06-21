# Dry-Run (Demo) Mode — Design

- **Date:** 2026-06-21
- **Status:** Approved (pending spec review)
- **Component:** `hypertrade/config.py`, `hypertrade/routes/webhooks.py`, `hypertrade/daemon.py`, `.env.example`, `tests/test_webhook.py`

## 1. Problem

There is no way to point a live TradingView strategy (or a manual `curl`) at
the daemon and confirm the **full receive→parse→validate→signal-map** pipeline
works without actually placing an order on Hyperliquid. Today the only path
through `/webhook` for an actionable signal ends in a real
`place_order` network call (`webhooks.py:229`), plus DB writes, idempotency
store writes, and a Telegram notification. Operators want a "demo" switch to
exercise the pipeline against real payloads while touching nothing external.

## 2. Goal / Non-goals

**Goal:** A daemon-level switch, `HYPERTRADE_DRY_RUN=true`, that makes
`/webhook` run the entire request pipeline up to and including building the
`OrderRequest`, then return a `dry_run` response **without any side effect** —
no Hyperliquid call, no DB row, no idempotency store write, no Telegram message.

**Non-goals:**
- No new CLI launcher / argparse entrypoint — the daemon is configured via env
  vars and launched by uvicorn; `HYPERTRADE_DRY_RUN` follows that convention.
- No per-request dry-run override (e.g. a flag inside the webhook payload).
- No simulated fills, fake order IDs, or synthetic prices beyond echoing the
  request — that would drift into trading/position logic this executor must not
  own (it stays a thin executor).
- No change to authentication, rate limiting, or the `/history` and admin routes.

## 3. Design

### 3.1 Config — `hypertrade/config.py`

Add one field to `Settings`:

```python
# Dry-run / demo: accept and fully validate webhooks but never place orders,
# write to the DB, touch the idempotency store, or send Telegram messages.
dry_run: bool = False
```

Default `False` ⇒ current behavior unchanged. Enabled with
`HYPERTRADE_DRY_RUN=true`. No validator needed: Pydantic handles bool coercion
and the existing `env_prefix="HYPERTRADE_"` maps the variable automatically.

### 3.2 Injection point — `hypertrade/routes/webhooks.py`

Insert a branch **after** `order_request` is constructed (`webhooks.py:~192`)
and **before** the `HyperliquidService` is constructed and the idempotency /
execution block runs:

```python
if settings.dry_run:
    log.info(
        "DRY-RUN: order NOT placed | %s %s qty=%s price=%s lev=%sx reduce_only=%s",
        symbol, side.value, order_request.qty, order_request.price,
        order_request.leverage or 1, order_request.reduce_only,
    )
    return _build_dry_run_response(
        payload, signal=signal, side=side, symbol=symbol, order_request=order_request
    )
```

**Why here:** at this point the request has already exercised everything worth
testing — JSON content-type check, JSON body parse, JSON-schema validation,
secret/auth enforcement, Pydantic validation, `parse_signal`, `signal_to_side`,
leverage parsing, `reduce_only` resolution, and `OrderRequest` construction.
The branch skips only the side-effecting tail: client (SDK) construction,
idempotency `reserve`/`complete`, `place_order`, DB `log_order`/`log_failure`,
and the Telegram `background_tasks` call.

**Unaffected paths:**
- The "no_action → ignored" early return (`webhooks.py:125`) is already
  side-effect-free and behaves identically in dry-run.
- The "nonce required" check (`webhooks.py:109`) stays **before** the branch:
  in dry-run the payload is still fully validated; only the idempotency *store
  writes* are skipped (because the branch returns before `reserve`).

### 3.3 Dry-run response — new helper `_build_dry_run_response`

A mirror of `_build_response`, returning the order that *would* have been sent
so the operator can verify mapping correctness at a glance:

```python
def _build_dry_run_response(
    payload: TradingViewWebhook, *, signal: SignalType, side: Side,
    symbol: str, order_request: OrderRequest,
) -> dict:
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

`"status": "dry_run"` is a new value alongside the existing `ok` / `ignored` /
`duplicate`.

### 3.4 Startup visibility — `hypertrade/daemon.py`

In `create_daemon`, when `settings.dry_run` is true, log a **WARNING** banner so
a demo daemon is never mistaken for a live one:

```
⚠️  DRY-RUN MODE ENABLED — webhooks are validated but NO orders are sent to
    Hyperliquid, NO DB writes, NO Telegram notifications.
```

## 4. Behavior

| `HYPERTRADE_DRY_RUN` | Actionable webhook | No-action webhook |
| --- | --- | --- |
| `false` (default) | order placed; DB + idempotency + Telegram as today | `200 {"status":"ignored"}` |
| `true` | `200 {"status":"dry_run", ...}`; no order, no DB, no idempotency, no Telegram | `200 {"status":"ignored"}` (unchanged) |

Validation/auth failures (415/422/401/400) behave identically in both modes —
dry-run never relaxes input validation.

## 5. Testing — `tests/test_webhook.py`

- With `dry_run=True`, a valid actionable webhook returns `200` and
  `body["status"] == "dry_run"`, and the echoed `symbol`/`side`/`contracts`
  match the payload.
- `HyperliquidService.place_order` is **never** called (spy/mock) in dry-run.
- No DB rows are written in dry-run (orders + failures counts unchanged).
- Regression: with `dry_run` unset/false, the existing place-order path still
  works (existing tests cover this).

Reuse the existing webhook test harness (the `make_app` fixture that sets
`HYPERTRADE_WEBHOOK_SECRET`), adding `HYPERTRADE_DRY_RUN=true`.

## 6. Documentation

- `.env.example`: add `HYPERTRADE_DRY_RUN=false` with a one-line comment.
- README: a short note that `HYPERTRADE_DRY_RUN=true` validates webhooks without
  trading — useful for wiring up a strategy before going live.

## 7. Self-critique

- The dry-run response echoes the request but carries no broker-assigned data
  (order id, fill price) — by design, since the executor must not fabricate
  trading state. Operators testing *fill* behavior still need testnet.
- A daemon-level switch means you cannot dry-run a single symbol while trading
  others live in the same process; that is an accepted simplification (run a
  second daemon in dry-run if needed).
- Placing the branch after `OrderRequest` construction maximizes pipeline
  coverage but means a future side-effect added *before* that point would also
  run in dry-run; the injection point should be revisited if the pre-build
  section ever gains an external call.
