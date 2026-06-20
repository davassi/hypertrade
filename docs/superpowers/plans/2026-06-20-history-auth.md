# Auth on `/history` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Require `Authorization: Bearer <HYPERTRADE_WEBHOOK_SECRET>` on all `/history/*` endpoints via a shared dependency reused by admin.

**Architecture:** Extract the Bearer-secret check (currently inline in `admin.py`) into `require_bearer_secret` in `hypertrade/security.py`, attach it to `history_router` as a router-level dependency, and repoint admin at the shared function (DRY).

**Tech Stack:** Python 3.10+, FastAPI dependencies, `hmac`, pytest + `fastapi.testclient`.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-06-20-history-auth-design.md`.
- Auth: `Authorization: Bearer <HYPERTRADE_WEBHOOK_SECRET>`, compared with `hmac.compare_digest`.
- Status codes: `200` valid; `401` missing/malformed/wrong token; `403` when no secret is configured.
- Applies to all four `/history/*` routes; no change to `/webhook` auth.
- Run tests with: `python3.11 -m pytest -p no:warnings -q`.
- Commit messages end with: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

---

### Task 1: Shared Bearer dependency, guard `/history`, DRY admin

**Files:**
- Modify: `hypertrade/security.py` (add `import hmac`; add `require_bearer_secret`)
- Modify: `hypertrade/routes/webhooks.py` (`history_router` creation at `:37`; import)
- Modify: `hypertrade/routes/admin.py` (remove local `_validate_webhook_secret` + unused `import hmac`; import and call the shared dependency at `:88`)
- Modify: `README.md`
- Test: `tests/test_webhook.py`

**Interfaces:**
- Produces: `hypertrade.security.require_bearer_secret(request: Request) -> None` — a FastAPI-dependency-compatible callable raising `HTTPException` (401/403) or returning `None`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_webhook.py`:

```python
def test_history_requires_bearer_auth(monkeypatch):
    app = make_app(monkeypatch, secret="secret")
    client = TestClient(app)
    assert client.get("/history/orders").status_code == 401                                    # missing
    assert client.get("/history/orders", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/history/stats", headers={"Authorization": "Bearer secret"}).status_code == 200
    assert client.get("/history/orders", headers={"Authorization": "Bearer secret"}).status_code == 200


def test_admin_auth_still_enforced_via_shared_dependency(monkeypatch):
    app = make_app(monkeypatch, secret="secret")
    client = TestClient(app)
    # No token → rejected by the shared dependency (regression after the DRY refactor)
    assert client.post("/admin/telegram", json={"enabled": False}).status_code == 401
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3.11 -m pytest tests/test_webhook.py -k "history_requires_bearer or admin_auth_still" -p no:warnings -q`
Expected: FAIL — `/history/*` currently returns `200` without a token (no auth yet).

- [ ] **Step 3: Add the shared dependency** — in `hypertrade/security.py`, add `import hmac` under the existing imports, then add this function:

```python
def require_bearer_secret(request: Request) -> None:
    """Require `Authorization: Bearer <webhook_secret>`.

    Raises 403 if no webhook secret is configured (the resource cannot be
    unlocked), 401 if the header is missing/malformed or the token is wrong.
    """
    settings = request.app.state.settings
    env_secret = getattr(settings, "webhook_secret", None)
    if not env_secret:
        raise HTTPException(status_code=403, detail="Forbidden: webhook secret not configured")

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Unauthorized: missing Bearer token")

    provided = auth_header[7:]  # strip "Bearer "
    if not hmac.compare_digest(provided, env_secret.get_secret_value()):
        raise HTTPException(status_code=401, detail="Unauthorized: invalid secret")
```

- [ ] **Step 4: Guard the history router** — in `hypertrade/routes/webhooks.py`, update the import of security helpers to include the new dependency, e.g. change `from ..security import require_ip_whitelisted` to:

```python
from ..security import require_ip_whitelisted, require_bearer_secret
```

and change the `history_router` creation (`:37`) to:

```python
history_router = APIRouter(tags=["history"], dependencies=[Depends(require_bearer_secret)])
```

(`Depends` is already imported in `webhooks.py`.)

- [ ] **Step 5: Repoint admin at the shared dependency** — in `hypertrade/routes/admin.py`:
  - delete the local `def _validate_webhook_secret(request: Request) -> None:` function (the whole block);
  - remove the now-unused `import hmac`;
  - add `from ..security import require_bearer_secret` with the other imports;
  - at the call site (`:88`), replace `_validate_webhook_secret(request)` with `require_bearer_secret(request)`.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `python3.11 -m pytest tests/test_webhook.py -k "history_requires_bearer or admin_auth_still" -p no:warnings -q`
Expected: PASS (2 tests).

- [ ] **Step 7: Run the full suite (no regressions)**

Run: `python3.11 -m pytest -p no:warnings -q`
Expected: PASS (all tests, including the existing admin/telegram tests).

- [ ] **Step 8: Document it** — in `README.md`, under the Endpoints / history area, add:

```markdown
The `/history/*` endpoints require authentication with the webhook secret:

```bash
curl -H "Authorization: Bearer $HYPERTRADE_WEBHOOK_SECRET" http://localhost:6487/history/stats
```

Requests without a valid `Authorization: Bearer <secret>` get `401`; if no
`HYPERTRADE_WEBHOOK_SECRET` is configured, `/history` returns `403`.
```

- [ ] **Step 9: Commit**

```bash
git add hypertrade/security.py hypertrade/routes/webhooks.py hypertrade/routes/admin.py README.md tests/test_webhook.py
git commit -m "feat(security): require Bearer auth on /history (shared dependency)"
```

---

## Self-Review

**Spec coverage:**
- §3 shared `require_bearer_secret` in security.py → Step 3. ✅
- §3 apply to history_router → Step 4. ✅
- §3 DRY admin onto the shared dependency → Step 5. ✅
- §4 behavior (200/401/403) → Steps 1 & 3 (the 403-no-secret path is in the dependency; covered by reusing the admin's existing 403 semantics). ✅
- §5 tests (history no-auth/wrong/valid, admin regression) → Step 1. ✅
- §6 README → Step 8. ✅

**Placeholder scan:** No TBD/TODO/"similar to". Step 5 is prose (a delete + an import swap) but names the exact symbols and line; every code-adding step shows the literal code.

**Type consistency:** `require_bearer_secret(request: Request) -> None` is defined in Step 3 and referenced identically in Steps 4 (import + `Depends`) and 5 (admin import + call). The Bearer/hmac/status-code semantics match the spec's §4 table.
