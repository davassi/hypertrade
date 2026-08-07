# Changelog

All notable changes to HyperTrade are documented here.
The format loosely follows [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

### Fixed — Hyperliquid k-contracts were unreachable (2026-08-07)

An order for `kFLOKI` was upper-cased to `KFLOKI`, which does not exist on
Hyperliquid, and the exchange rejected it with
`Symbol 'KFLOKI' not found in Hyperliquid universe`. In production this left the
calling desk holding a filled AZTEC leg against a rejected kFLOKI leg, which it
had to unwind. All seven 1000x contracts (`kPEPE`, `kSHIB`, `kBONK`, `kLUNC`,
`kFLOKI`, `kDOGS`, `kNEIRO`) were affected — 7 of the venue's 232 main-perp coins.

The venue is case-sensitive and lists `KAS` and `KAITO` alongside the k-contracts,
so no lexical rule can decide the right spelling. Both `.upper()` call sites are
replaced by one shared module, `hypertrade/routes/symbols.py`:

- `normalize_symbol` — the boundary rule where the venue is out of reach (the
  webhook handler runs before any client exists, and dry-run returns without
  building one). Upper-cases free text; never destroys case a caller supplied.
- `canonical_symbol` / `HyperliquidDataClient.canonical_symbol` — resolves against
  the venue universe, which is the authority. Exact hit wins, so a caller's precise
  spelling is never swapped for a case-folded sibling; two markets differing only
  by case with no exact hit raise rather than guess. Dex-aware, and reuses the
  per-dex meta memo, so it adds no round-trip on the order path.

`place_order` now resolves the symbol before anything is keyed by it, and
**reports the spelling it actually traded** on its result. The webhook records and
returns that value: previously both layers applied the same idempotent rule, so
reported-equalled-traded for free; once the layers differ, recording the handler's
version would name a coin that was never traded and show up as an orphan in the
desk's reconciler.

A universe-fetch failure propagates instead of degrading to a guess — the guess
reaches the exchange as 'not found', which the retry loop treats as permanent, so
one dropped POST would turn a fillable order into a terminal reject.

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

### Fixed

- **TD-18** — the *invalid-JSON* path (`_log_invalid_json_body`) no longer leaks
  the webhook secret. The raw body, logged at WARNING for debugging, is now passed
  through `_redact_secrets`, which masks any `"secret": "…"` field and the
  configured secret value before logging. Surfaced during the order-failure-logging
  review and fixed on the same branch.
