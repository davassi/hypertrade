# Interactive Setup Wizard — Design

- **Date:** 2026-06-19
- **Status:** Approved (pending spec review)
- **Component:** new `hypertrade/setup.py`, launcher scripts, daemon banner, README

## 1. Problem

A first-time operator who runs the daemon without the required environment
variables gets a static banner (`_please_die_gracefully` in `daemon.py`) telling
them which vars to set, then the process exits. There is no guided way to
actually *set* them. We want a text-based tutorial that, when configuration is
missing, prompts for the values and persists them.

## 2. Goal / Non-goals

**Goal:** An interactive, text-based setup that collects the **minimum config
needed to start** (only the values that are missing) and persists them, invoked
from the existing launcher scripts before `uvicorn` starts.

**Non-goals:** operational settings (listen_port, db_path, Telegram, rate limit,
trusted hosts, premium bps) — not prompted. No GUI. No remote/secrets-manager
backends beyond `pass` and `.env`.

## 3. Prompted values (minimum to start)

Only values that are **not already resolvable** are prompted (idempotent).

| Value | Prompt | Validation |
| --- | --- | --- |
| `environment` | `prod` / `test` | must be exactly `prod` or `test` |
| `master_addr` | Hyperliquid master address | `0x` + 40 hex chars |
| `api_wallet_priv` | API wallet private key | **hidden input (`getpass`)**; 64 hex chars, optional `0x` prefix |
| `subaccount_addr` | sub-account (optional) | empty → skip; else `0x` + 40 hex |
| **auth (choose one)** | `secret` or `whitelist` | — |
| └ `webhook_secret` | shared secret | hidden input; non-empty |
| └ IP whitelist | enable + allowed IPs | sets `ip_whitelist_enabled=true`; IPs as a JSON list (validated as IPv4) |

Invalid input re-prompts (bounded loop) rather than aborting.

## 4. Persistence backend (auto-detected)

`environment` selects the `pass` namespace prefix, matching the existing
launchers: **prod → `hypertrade/`**, **test → `hypertrade_test/`**.

- **`pass` available** (`shutil.which("pass")` is set **and** a usable store
  exists — `~/.password-store/.gpg-id` present or `pass ls` exits 0):
  store secrets via `pass insert -m <prefix><key>` for `master_addr`,
  `api_wallet_priv`, `subaccount_addr`, and the chosen auth secret.
- **`pass` absent:** print an **install recommendation** (e.g.
  `apt install pass` / `brew install pass`, then `pass init <gpg-id>`), then
  ask `Continue with a .env file instead? [Y/n]`. On yes, write a `.env`
  (mode `0600`) with the `HYPERTRADE_*` keys (including `HYPERTRADE_ENVIRONMENT`).
  On no, exit with instructions to install `pass` and re-run.

The `.env` path is Pydantic-native (`Settings` already reads `.env`); the `pass`
path matches the launchers (which export from `pass`).

## 5. Components & boundaries

Separate **pure logic** (testable without a terminal) from **I/O** (prompts):

- `hypertrade/setup.py`:
  - Pure: `validate_environment`, `validate_address`, `validate_privkey`,
    `validate_ipv4`; `missing_values(resolved: dict) -> list[str]`;
    `pass_available() -> bool`; `pass_key(environment, name) -> str`;
    `render_env_file(values: dict) -> str`.
  - I/O shell: `run()` orchestrates prompts (`input`/`getpass`), calls the pure
    helpers, persists via `pass` (subprocess) or `.env`, prints guidance.
  - Entry point: `python -m hypertrade.setup` (a `__main__` block calling `run()`).
- **Launchers** (`hypertrade-{prod,test}.{sh,fish}`): before `uvicorn`, if the
  required values do not resolve (e.g. `pass show <prefix>master_addr` fails),
  run `python -m hypertrade.setup` (the launcher already knows prod/test, so it
  passes that in to skip the environment prompt), then continue. The launchers
  also export the chosen auth secret (currently they export only
  master/priv/subaccount).
- **Daemon banner**: `_please_die_gracefully` gains a line pointing to
  `python -m hypertrade.setup`.
- **README**: a "Guided setup" subsection documenting `python -m hypertrade.setup`
  and the launchers' auto-setup, plus the `pass`-or-`.env` behavior.

## 6. Error handling

- Every prompt validates and re-prompts on bad input (bounded retries, then a
  clear abort message).
- `pass insert` failures (GPG locked, no key) are caught and reported with the
  exact failing key; the wizard offers the `.env` fallback rather than crashing.
- `.env` is written atomically (temp file + rename) with `0600` perms; an
  existing `.env` is appended-to / updated key-by-key, never blindly truncated.
- The private-key prompt never echoes and is never logged.

## 7. Testing

Pure logic (no TTY):
- validators accept good values and reject bad ones (env, address, privkey, ipv4);
- `missing_values` returns exactly the unset required keys;
- `pass_available` true/false by mocking `shutil.which` + store check;
- `pass_key("prod", "master_addr") == "hypertrade/master_addr"` and the `test`
  prefix;
- `render_env_file` produces valid `HYPERTRADE_*` lines including environment.

I/O shell (injected input):
- a full run with `pass` mocked records the expected `pass insert` calls;
- a full run with `pass` absent writes a `.env` with `0600` and the right keys;
- invalid-then-valid input re-prompts and ultimately succeeds.

## 8. Self-critique

- Editing four launcher scripts (bash+fish × prod+test) duplicates the
  pre-flight check across two shell dialects — unavoidable given the existing
  layout; kept minimal (a small guard block) rather than introducing a shared
  helper script, to stay within scope.
- Detecting "config missing" in the launcher by probing `pass show` couples the
  launcher to the `pass` layout; acceptable since the launchers are already
  `pass`-specific. The `.env`-only operator runs `python -m hypertrade.setup`
  directly (documented), since the `pass`-based launchers are not their path.
- The wizard writes secrets; it must never echo the private key or log values.
  Enforced via `getpass` and by not passing secrets through logging.
