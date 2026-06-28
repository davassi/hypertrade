# Changelog

All notable changes to HyperTrade are documented here.
The format loosely follows [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

### Added — Order-failure diagnostic logging (2026-06-28)

When an order fails, the logs now carry enough context to reconstruct the root
cause. Observability only — no change to the error taxonomy, retry policy, HTTP
status codes, DB schema, or pricing/submission logic.

- **Correlation across all layers.** `OrderRequest` now carries a `req_id`
  (alongside the existing `cloid`); every failure log line in the execution path
  is tagged so it can be tied back to the originating webhook request and to the
  exchange-side order id. `cloid` threads execution-client → service → webhook →
  exchange; `req_id` threads webhook → service.
- **Submitted price is logged before every order.** `market_order` / `limit_order`
  emit one INFO line with `mid`, slippage bps, the aggressive price, and the
  tick-normalized price actually sent — so a "bad price / could not match" reject
  has the exact price on the immediately preceding line.
- **Service-layer rejects log full context + payload.** The five `place_order`
  failure sites (leverage-reject, no-response, unexpected-shape, exchange-reject,
  unexpected-status) now log the order context (symbol, side, size, reduce_only,
  leverage, cloid, req_id) plus the full exchange response via a defensive
  `_safe_json` (falls back to `repr` so a serialisation error can never mask the
  original failure).
- **Retry loop / webhook handlers enriched.** The terminal network/API retry line
  now includes the underlying error string (previously it logged only the attempt
  count); the three exception handlers carry `req_id`/`cloid`/`symbol`/`side`.
- **Shared helper.** A single `format_log_context(**fields)` (in
  `hypertrade/logging.py`) builds the uniform `key=value` context, skipping
  `None`, used by all three layers (no copy-pasted prefix).
- **Secret-safety guard.** A regression test asserts the webhook secret never
  appears in failure-level (WARNING+) logs on the order-execution path.

Design: `docs/superpowers/specs/2026-06-28-order-failure-logging-design.md` ·
Plan: `docs/superpowers/plans/2026-06-28-order-failure-logging.md`.

### Noted

- **TD-18** (tech-debt register): the *invalid-JSON* path
  (`_log_invalid_json_body`) still logs the raw request body at WARNING, which can
  expose `general.secret` on a malformed payload. Pre-existing and out of scope of
  the above (which hardened the order path); tracked for a follow-up redaction.
