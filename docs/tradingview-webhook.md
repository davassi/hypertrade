# TradingView Webhook Reference

How HyperTrade interprets a TradingView alert payload, what each field means, and
ready-to-use example payloads for every trading signal.

- **Endpoint:** `POST /webhook`
- **Required header:** `Content-Type: application/json` (otherwise `415`)
- **Validation:** the raw JSON is checked against a JSON Schema
  (`hypertrade/schemas/tradingview_schema.py`) and then parsed into a Pydantic
  model (`hypertrade/schemas/tradingview.py`).

For the live TradingView alert template (with `{{...}}` placeholders), see the
[`TradingView Webhook Payload`](../README.md#tradingview-webhook-payload) section
of the README. This document focuses on **field semantics**, **signal
derivation**, and **concrete examples**.

---

## 1. Payload structure

The payload has four required objects: `general`, `currency`, `order`, `market`.

| Object | Field | Required | Type / values | Notes |
| --- | --- | --- | --- | --- |
| `general` | `ticker` | ✅ | string | Used only for logging/notifications. |
| | `interval` | ✅ | string | e.g. `"60"`. Informational. |
| | `time` | ✅ | string (ISO 8601 date-time) | Bar time. |
| | `timenow` | ✅ | string (ISO 8601 date-time) | Alert fire time. |
| | `strategy` | ➖ | string | Free-text strategy name. |
| | `secret` | ➖ | string | Required **only** if `HYPERTRADE_WEBHOOK_SECRET` is set (see §4). |
| | `leverage` | ➖ | string | `"3x"`, `"3X"` or `"3"` → parsed to int. Invalid → `400`. |
| `currency` | `base` | ✅ | string | **Drives the traded symbol**: `symbol = base.upper()`. |
| | `quote` | ➖ | string | Informational. |
| `order` | `action` | ✅ | `"buy"` \| `"sell"` | One half of the signal derivation. |
| | `contracts` | ✅ | string \| number | Order quantity. |
| | `price` | ✅ | string \| number | Used for `notional = contracts × price`. |
| | `id` | ✅ | string | Echoed back in the response. |
| | `comment` | ➖ | string \| null | Informational. |
| | `alert_message` | ➖ | string \| null | Informational. |
| `market` | `position` | ✅ | `"long"` \| `"short"` \| `"flat"` | **Target** position. |
| | `position_size` | ✅ | string \| number | Informational. |
| | `previous_position` | ✅ | `"long"` \| `"short"` \| `"flat"` | **Prior** position — half of the signal derivation. |
| | `previous_position_size` | ✅ | string \| number | Informational. |

> **The traded symbol comes from `currency.base`, not `general.ticker`.**
> `general.ticker` is only logged. (`hypertrade/routes/webhooks.py` → `symbol = payload.currency.base.upper()`.)

---

## 2. How the signal is derived

There is **no explicit "signal" field** in the payload. HyperTrade derives a
normalized `SignalType` from the state transition
`previous_position → position` combined with `order.action`
(`parse_signal()` in `hypertrade/routes/webhooks.py`). This mirrors the values
TradingView exposes natively (`{{strategy.market_position}}`,
`{{strategy.prev_market_position}}`, `{{strategy.order.action}}`).

| `previous_position` | `position` | `order.action` | Derived signal | Order side | `reduce_only` |
| --- | --- | --- | --- | --- | --- |
| `flat` | `long` | `buy` | `OPEN_LONG` | buy | no |
| `long` | `flat` | `sell` | `CLOSE_LONG` | sell | no |
| `flat` | `short` | `sell` | `OPEN_SHORT` | sell | no |
| `short` | `flat` | `buy` | `CLOSE_SHORT` | buy | no |
| `long` | `long` | `buy` | `ADD_LONG` | buy | no |
| `long` | `long` | `sell` | `REDUCE_LONG` | sell | **yes** |
| `short` | `short` | `sell` | `ADD_SHORT` | sell | no |
| `short` | `short` | `buy` | `REDUCE_SHORT` | buy | **yes** |
| `short` | `long` | _(any)_ | `REVERSE_TO_LONG` | buy | no |
| `long` | `short` | _(any)_ | `REVERSE_TO_SHORT` | sell | no |
| _anything else_ | | | `NO_ACTION` | — | — |

When the derived signal is `NO_ACTION` (or no side resolves), the request is
accepted with `{"status": "ignored", "reason": "no_action", ...}` and **no order
is placed**. `REDUCE_*` signals set `reduce_only=true` so they can only shrink an
existing position, never open a new one.

The derived signal is surfaced in the HTTP response (`"signal": "..."`), the
order log table, and the optional Telegram notification — so you never need to
declare it in the inbound payload.

---

## 3. Example payloads

`general` and `currency` are identical across examples; only `order.action` and
`market` change. Replace `"YOUR_SECRET"` with your `HYPERTRADE_WEBHOOK_SECRET`
(or omit `general.secret` if you authenticate via IP whitelist).

All examples below were validated against the schema and confirmed to produce
the stated signal via `parse_signal()`.

### OPEN_LONG — open a long
```json
{
  "general": { "strategy": "My Strategy", "ticker": "SOLUSD", "interval": "60", "time": "2026-06-18T06:00:00Z", "timenow": "2026-06-18T06:00:01Z", "secret": "YOUR_SECRET", "leverage": "3x" },
  "currency": { "base": "SOL", "quote": "USD" },
  "order": { "action": "buy", "contracts": "10", "price": "150.0", "id": "Long Entry", "comment": "open long", "alert_message": "" },
  "market": { "position": "long", "position_size": "10", "previous_position": "flat", "previous_position_size": "0" }
}
```

### CLOSE_LONG — close the long
```json
{
  "general": { "strategy": "My Strategy", "ticker": "SOLUSD", "interval": "60", "time": "2026-06-18T06:00:00Z", "timenow": "2026-06-18T06:00:01Z", "secret": "YOUR_SECRET", "leverage": "3x" },
  "currency": { "base": "SOL", "quote": "USD" },
  "order": { "action": "sell", "contracts": "10", "price": "150.0", "id": "Long Exit", "comment": "close long", "alert_message": "" },
  "market": { "position": "flat", "position_size": "0", "previous_position": "long", "previous_position_size": "10" }
}
```

### OPEN_SHORT — open a short
```json
{
  "general": { "strategy": "My Strategy", "ticker": "SOLUSD", "interval": "60", "time": "2026-06-18T06:00:00Z", "timenow": "2026-06-18T06:00:01Z", "secret": "YOUR_SECRET", "leverage": "3x" },
  "currency": { "base": "SOL", "quote": "USD" },
  "order": { "action": "sell", "contracts": "10", "price": "150.0", "id": "Short Entry", "comment": "open short", "alert_message": "" },
  "market": { "position": "short", "position_size": "10", "previous_position": "flat", "previous_position_size": "0" }
}
```

### CLOSE_SHORT — close the short
```json
{
  "general": { "strategy": "My Strategy", "ticker": "SOLUSD", "interval": "60", "time": "2026-06-18T06:00:00Z", "timenow": "2026-06-18T06:00:01Z", "secret": "YOUR_SECRET", "leverage": "3x" },
  "currency": { "base": "SOL", "quote": "USD" },
  "order": { "action": "buy", "contracts": "10", "price": "150.0", "id": "Short Exit", "comment": "close short", "alert_message": "" },
  "market": { "position": "flat", "position_size": "0", "previous_position": "short", "previous_position_size": "10" }
}
```

### Advanced signals (deltas only)

Keep `general`/`currency` as above and set `order.action` + `market` like this:

| Signal | `order.action` | `previous_position` | `position` |
| --- | --- | --- | --- |
| `ADD_LONG` | `buy` | `long` | `long` |
| `REDUCE_LONG` | `sell` | `long` | `long` |
| `ADD_SHORT` | `sell` | `short` | `short` |
| `REDUCE_SHORT` | `buy` | `short` | `short` |
| `REVERSE_TO_LONG` | `buy` | `short` | `long` |
| `REVERSE_TO_SHORT` | `sell` | `long` | `short` |

---

## 4. Authentication

At least one method must be enabled (the daemon refuses to start otherwise):

- **Shared secret** — set `HYPERTRADE_WEBHOOK_SECRET`. The payload's
  `general.secret` must match (constant-time compare); otherwise `401`.
- **IP whitelist** — set `HYPERTRADE_IP_WHITELIST_ENABLED=true` and
  `HYPERTRADE_TV_WEBHOOK_IPS` (JSON array). See the README for details.

---

## 5. Sending a payload

```bash
curl -X POST http://localhost:6487/webhook \
  -H "Content-Type: application/json" \
  -d @open_long.json
```

Use your configured `HYPERTRADE_LISTEN_PORT` if it differs from `6487`.

A successful order returns:

```json
{
  "status": "ok",
  "signal": "OPEN_LONG",
  "side": "buy",
  "symbol": "SOL",
  "ticker": "SOLUSD",
  "action": "buy",
  "contracts": "10",
  "price": "150.0",
  "received_at": "2026-06-18T06:00:01.123456+00:00"
}
```
