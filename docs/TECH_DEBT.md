# Tech Debt Register

Living list of known structural / maintainability / correctness debt in HyperTrade.

**Scope rule:** this service stays a *thin executor*. Items that would add
trading / position / risk logic (sizing, balance caps, stop-loss, …) belong to
the strategy bot and are intentionally absent here.

**Severity:** `P0` correctness on the money path under load · `P1` should fix ·
`P2` cleanup / hygiene.

Entries are point-in-time observations — verify file references against current
code before acting.

---

## Open

### Correctness

- **[TD-2] Domain `ValueError`/`KeyError` bypass the error taxonomy** (`P1`) —
  symbol-not-found (`hyperliquid_data_client._symbol_to_idx`), missing
  `withdrawable` (`get_available_balance`), and the response-parsing chain
  `res["response"]["data"]["statuses"][0]` in `hyperliquid_service.py` raise raw
  exceptions that escape `_place_order_with_retry` → unhandled 500 instead of
  400/validation. *Fix:* map the user-facing ones to
  `HyperliquidValidationError` / `HyperliquidAPIError`. (Follow-up to `a8facf4`,
  which covered only transport errors.)

### Concurrency / scale

- **[TD-3] Rate limiter is per-process** (`P1`) —
  `middleware/rate_limit.py` keeps counters in an in-process dict, so
  `uvicorn --workers N` multiplies the effective limit by N; the per-IP dict
  also never evicts (unbounded memory). The design is single-process.
  *Fix:* document/enforce a single worker and bound/evict the dict. **Not** Redis.

### Error handling

- **[TD-5] Telegram send swallows only `ApiTelegramException`** (`P1`) —
  `notify.py` lets `requests` network errors escape the fire-and-forget
  background task, violating its `bool` contract and spewing tracebacks on a
  flaky endpoint. *Fix:* broaden the catch and return `False`.

- **[TD-16] Schema-validation 422 hides the offending field** (`P2`) —
  `routes/webhooks.py::_validate_schema` catches `JSONSchemaValidationError` and
  re-raises `HTTPException(422, detail="JSON schema validation error")`,
  discarding the underlying field path and constraint — and it does not log them
  either, so neither the response nor the server log says *what* failed. An
  integrator gets an opaque 422 and must re-run the validator offline to find the
  cause (observed: a 780-payload basket that all 422'd solely on
  `general.secret` `minLength:1`, an empty-string secret that the sender should
  have omitted). *Fix:* surface the failing **field path + constraint** only —
  e.g. `"/".join(map(str, exc.absolute_path))` + `exc.validator` — in `detail`
  and/or a WARNING log.
  **Do not** echo `exc.message` or the instance value: schema errors quote the
  offending value, and `general.secret` is a credential (would leak the secret
  on a length/format mismatch).

### Security / hygiene

- **[TD-7] `telegram_bot_token` is a plain `str`, not `SecretStr`** (`P2`) —
  `config.py`. A bot token is a credential; the other secrets use `SecretStr`
  and the admin endpoint already masks it. *Fix:* `Optional[SecretStr]`, call
  `.get_secret_value()` at the single send site.

- **[TD-8] `trusted_hosts` defaults to `["*"]`** (`P2`) —
  with `enable_trusted_hosts=true` the TrustedHostMiddleware allows every
  `Host`: a guard that looks enabled but is wide open. *Fix:* a validator that
  errors/warns on `["*"]` while the feature is enabled.

- **[TD-9] Webhook secret travels in the JSON body** (`P2`, note-only) —
  `general.secret` (vs the `Authorization: Bearer` header used by `/history`).
  Dictated by TradingView's webhook format (no custom headers); the comparison
  is already timing-safe (`hmac.compare_digest`). Accepted; revisit only if a
  header path becomes possible.

### Maintainability

- **[TD-10] `routes/webhooks.py` is 751 lines** (`P2`) —
  mixes the webhook handler + helpers with 4 read-only history endpoints
  (`history_router`, which has its own auth dependency). Clean seam: extract
  `history_router` into `routes/history.py` (~130 lines, near-zero coupling).
  Don't split further — the rest is cohesive.

- **[TD-11] Setup wizard never sets `trust_forwarded_for`** (`P2`) —
  `setup.py` collects the IP whitelist but not
  `HYPERTRADE_TRUST_FORWARDED_FOR`; behind a reverse proxy that silently 403s
  every whitelisted client. *Fix:* prompt for the proxy case, or document it in
  the wizard output.

- **[TD-12] `api_url` re-validates an already-validated field** (`P2`, trivial) —
  the `config.py` property has an unreachable `else: raise` (the field validator
  already guarantees `prod`/`test`). *Fix:* simplify to a dict lookup.

### Tests / toolchain

- **[TD-14] Suite is env-sensitive and pytest runs only on python3.11** (`P2`) —
  the repo's `python3` is 3.14 (no pytest); some flows still depend on ambient
  `HYPERTRADE_*`. A real CI gap. *Fix:* a conftest/fixture that sets baseline env,
  and a pinned/tested interpreter. (Partly mitigated by TD-13's hermetic fixtures.)

- **[TD-15] Idempotency can't be load-tested in isolation** (`P2`, testability) —
  dry-run returns *before* `reserve()`, so the nonce/dedup path can't be stressed
  without hitting the exchange. *Fix:* a test-only "mock execution" seam,
  if/when needed.

---

## Resolved

- 2026-06-22 `5995bac` — **TD-17**: dropped `close_position`'s nested
  premium-escalation retry (it conflicted with cloid idempotency, and
  slippage-escalation policy belongs to the strategy bot). Single reduce-only
  IOC now.
- 2026-06-22 `2c27b08` — **TD-1**: deterministic cloid (from nonce) +
  query-before-resubmit prevents double-submitting an order on retry.
- 2026-06-22 `8405351` — **TD-6**: per-order meta fetch memoized (5 → 1
  `metaAndAssetCtxs` POSTs), dead balance fetch dropped.
- 2026-06-22 `5ef2dc9` — **TD-4**: completed nonces swept by age, bounding the
  `idempotency_keys` table.
- 2026-06-22 `6786201` — **TD-13**: webhook/health test fixtures made hermetic;
  `master` is green regardless of ambient env.
- 2026-06-22 `a8facf4` — **P0 #1**: raw `requests` transport errors translated
  into the retry taxonomy (the dead network-retry branch is now reachable).
- 2026-06-22 `e3f57e1` — **P0 #2**: SQLite `busy_timeout` + WAL via a shared
  connection factory (concurrent "database is locked" under threaded order
  placement).
- 2026-06-22 `0c3a49c` — dead config fields & code removed, false "mock mode"
  docstring corrected, dead `default_premium_bps=5.0` dropped, app version
  single-sourced from `version.py`.
