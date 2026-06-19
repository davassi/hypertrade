# Bounded History Retention — Design

- **Date:** 2026-06-19
- **Status:** Approved (pending spec review)
- **Component:** `hypertrade` order/failure logging (`hypertrade/database.py`)

## 1. Problem

The `orders` and `failures` SQLite tables grow **without bound** — there is no
pruning, rotation, or retention policy (`database.py`). On a long-running bot
this saturates disk (observed in practice). `get_statistics` also `COUNT(*)`s
the full tables, so its "metrics" are all-time cumulative rather than recent.

## 2. Goal / Non-goals

**Goal:** Cap each of `orders` and `failures` to the most recent **N** rows
(default 200), rotating on insert, so the tables can never grow without bound.
As a side effect, `/history` and `/history/stats` reflect recent activity (the
last ≤N rows) — the "metrics" become meaningfully recent.

**Accepted trade-off:** order/failure history older than N rows is **lost**
(no long-term audit). This was explicitly chosen over unbounded growth.

**Non-goals:**
- Richer metrics (latency percentiles, retry counts) or a `/metrics` endpoint.
- Retention for `idempotency_keys` (it grows slower and serves the dedup
  window) — a possible separate follow-up, out of scope here.

## 3. Design

**Mechanism — trim-on-insert.** After each successful INSERT, in the same
transaction, delete rows beyond the newest N (by the autoincrement `id`):

```sql
DELETE FROM orders   WHERE id NOT IN (SELECT id FROM orders   ORDER BY id DESC LIMIT :n);
DELETE FROM failures WHERE id NOT IN (SELECT id FROM failures ORDER BY id DESC LIMIT :n);
```

Both tables have `id INTEGER PRIMARY KEY AUTOINCREMENT`, so `id` is a monotonic
recency key. Trim-on-insert keeps each table at ≤N continuously, is
self-healing (the first insert prunes a table that grew large in the past), and
costs one indexed DELETE per insert — negligible at a trading bot's volume. The
cap is applied **per table** (N orders and N failures independently).

## 4. Configuration & wiring

- New setting `HYPERTRADE_MAX_HISTORY_ROWS: int = 200` (`config.py`). No
  hardcoded literal in the data layer.
- `OrderDatabase.__init__(self, db_path, max_rows: int = 200)` stores the cap.
- `daemon.py` constructs `OrderDatabase(settings.db_path, max_rows=settings.max_history_rows)`.
- `log_order` and `log_failure` run their trim DELETE using `self.max_rows`.

`/history`, `/history/stats`, `/history/order/{id}`, `/history/failures` are
**unchanged** — they query the (now bounded) tables as before.

## 5. Edge cases / notes

- `failures.order_id` is a FK to `orders(id)`. Trimming `orders` can leave a
  `failures` row pointing at a removed order id. SQLite does not enforce FKs
  here (no `PRAGMA foreign_keys=ON`), and these are analytics logs, so a
  dangling reference is harmless — noted for honesty.
- `max_rows` must be a positive int; a value `<= 0` would delete everything on
  insert. The setting is validated to be `>= 1`.

## 6. Testing

- Insert N+5 orders → table holds exactly N rows, and they are the newest
  (oldest 5 pruned). Same for failures.
- A configurable smaller cap (e.g. 3) is honored.
- Inserting fewer than N rows leaves them all intact.
- An existing over-full table is pruned to N on the next insert (self-heal).

## 7. Self-critique

- Losing old history is a real cost (no long-term audit/tax record). Accepted
  deliberately; if audit history is later needed, export-before-prune or a
  separate archive table would be the follow-up.
- Trim-on-insert ties retention to write activity: a table that is over-full
  but receives no new inserts stays over-full until the next write. For this
  bot, writes are frequent enough that this is not a concern.
