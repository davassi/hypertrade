# Auth on `/history` Endpoints — Design

- **Date:** 2026-06-20
- **Status:** Approved (pending spec review)
- **Component:** `hypertrade/security.py`, `hypertrade/routes/webhooks.py` (history_router), `hypertrade/routes/admin.py`, README

## 1. Problem

The read endpoints `/history/orders`, `/history/failures`, `/history/stats`,
and `/history/order/{request_id}` (on `history_router`, `webhooks.py:37,576+`)
have **no authentication** — any client that can reach the daemon can read the
full order history, including subaccount addresses and P&L. The admin endpoint
already authenticates with a Bearer secret, but that logic lives inline in
`admin.py` (`_validate_webhook_secret`) and is not reused.

## 2. Goal / Non-goals

**Goal:** Require `Authorization: Bearer <HYPERTRADE_WEBHOOK_SECRET>` on all
`/history/*` endpoints, by extracting the existing Bearer-secret check into a
shared dependency reused by both `/history` and admin (removing the inline
duplication).

**Non-goals:** rate limiting on `/history`; a dedicated history secret; IP
whitelist for `/history`; any change to the `/webhook` endpoint's auth.

## 3. Design

**Shared dependency.** Add to `hypertrade/security.py`:

```python
def require_bearer_secret(request: Request) -> None:
    """Require `Authorization: Bearer <webhook_secret>`.

    401 if the header is missing/malformed or the token is wrong; 403 if no
    webhook secret is configured (the resource cannot be unlocked).
    """
```

It reads `request.app.state.settings.webhook_secret`, compares the Bearer token
with `hmac.compare_digest`, and raises `HTTPException` with the same status
codes the admin check uses today (403 not-configured, 401 missing/invalid).

**Apply to `/history`.** Protect the whole router at creation
(`webhooks.py:37`):

```python
history_router = APIRouter(tags=["history"], dependencies=[Depends(require_bearer_secret)])
```

This guards all four GET routes uniformly; the handlers are unchanged.

**DRY the admin.** Replace `admin.py`'s local `_validate_webhook_secret` with an
import of `require_bearer_secret`; `manage_telegram_settings` calls the shared
function (it already calls the local one inline at `admin.py:88`). One auth
implementation, two consumers.

## 4. Behavior

| Request to `/history/*` | Result |
| --- | --- |
| Valid `Authorization: Bearer <secret>` | `200` (handler runs) |
| Missing header / not `Bearer ` / wrong token | `401` |
| No `HYPERTRADE_WEBHOOK_SECRET` configured | `403` |

> **Note (accepted):** a deployment that authenticates the webhook by **IP
> whitelist only** (no `webhook_secret`) will get `403` on `/history` until a
> `HYPERTRADE_WEBHOOK_SECRET` is set. "No secret → no access" is the safe
> default and is documented.

## 5. Testing

- `/history/orders` (and one other `/history` route) without `Authorization` → `401`.
- with a wrong Bearer token → `401`.
- with the correct Bearer token → `200`.
- admin endpoint still authenticates correctly through the shared dependency
  (regression: `401` without token, success with it).
- `require_bearer_secret` raises `403` when `webhook_secret` is unset.

The existing webhook test harness (`make_app`, which sets `HYPERTRADE_WEBHOOK_SECRET`)
is reused; tests add the `Authorization` header.

## 6. Documentation

README: note that `/history/*` requires `Authorization: Bearer <webhook_secret>`,
with a `curl` example.

## 7. Self-critique

- Coupling `/history` read-auth to the same `webhook_secret` used for write
  auth means one secret unlocks both. For a single-operator personal bot this
  is acceptable and simplest; a dedicated read secret (rejected here) would
  separate the roles if multi-tenant access ever matters.
- The `403`-when-unset behavior could surprise an IP-whitelist-only operator;
  mitigated by documentation, not by code.
