# Interactive Setup Wizard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A text-based `python -m hypertrade.setup` wizard that prompts for the minimum config to start the daemon and persists it to `pass` (if available) or a `.env` file, wired into the launcher scripts.

**Architecture:** A new `hypertrade/setup.py` splits pure, testable logic (validators, missing-detection, backend selection, `.env` rendering) from a thin interactive `run()` shell (prompts, `pass insert`/`.env` write). Launchers run it pre-flight when config is missing; the daemon banner and README point to it.

**Tech Stack:** Python 3.10+ stdlib (`getpass`, `subprocess`, `shutil`, `re`, `pathlib`), pytest, bash + fish launchers.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-06-19-setup-wizard-design.md`.
- Prompt **only the minimum to start**: `environment` (prod/test), `master_addr`, `api_wallet_priv` (hidden), optional `subaccount_addr`, and one auth method (`webhook_secret` OR IP whitelist). No operational settings.
- `pass` namespace by environment: prod → `hypertrade/`, test → `hypertrade_test/`.
- Backend: `pass` if available, else recommend installing `pass` and fall back to `.env` (mode `0600`).
- The private key is read with `getpass`, never echoed, never logged.
- Run tests with: `python3.11 -m pytest -p no:warnings -q`.
- Commit messages end with: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

---

### Task 1: Pure helpers & validators (`hypertrade/setup.py`)

**Files:**
- Create: `hypertrade/setup.py`
- Test: `tests/test_setup.py`

**Interfaces:**
- Produces: `validate_environment(str)->bool`, `validate_address(str)->bool`, `validate_privkey(str)->bool`, `validate_ipv4(str)->bool`, `missing_values(dict)->list[str]`, `pass_available()->bool`, `pass_key(environment:str, name:str)->str`, `render_env_file(dict)->str`, and module constant `REQUIRED_KEYS`.

- [ ] **Step 1: Write the failing tests** — create `tests/test_setup.py`:

```python
"""Tests for the interactive setup wizard's pure logic and I/O shell."""

from __future__ import annotations

import pathlib
import sys

REPO_ROOT = str(pathlib.Path(__file__).resolve().parents[1])
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from hypertrade import setup


def test_validate_environment():
    assert setup.validate_environment("prod")
    assert setup.validate_environment("test")
    assert not setup.validate_environment("staging")
    assert not setup.validate_environment("")


def test_validate_address():
    assert setup.validate_address("0x" + "a" * 40)
    assert not setup.validate_address("0x123")
    assert not setup.validate_address("a" * 40)


def test_validate_privkey():
    assert setup.validate_privkey("a" * 64)
    assert setup.validate_privkey("0x" + "A" * 64)
    assert not setup.validate_privkey("a" * 63)
    assert not setup.validate_privkey("xyz")


def test_validate_ipv4():
    assert setup.validate_ipv4("52.89.214.238")
    assert not setup.validate_ipv4("256.1.1.1")
    assert not setup.validate_ipv4("1.2.3")


def test_missing_values():
    resolved = {
        "HYPERTRADE_ENVIRONMENT": "test",
        "HYPERTRADE_MASTER_ADDR": "",
        "HYPERTRADE_API_WALLET_PRIV": "x",
    }
    assert setup.missing_values(resolved) == ["HYPERTRADE_MASTER_ADDR"]


def test_pass_key():
    assert setup.pass_key("prod", "master_addr") == "hypertrade/master_addr"
    assert setup.pass_key("test", "master_addr") == "hypertrade_test/master_addr"


def test_pass_available_false_without_binary(monkeypatch):
    monkeypatch.setattr(setup.shutil, "which", lambda name: None)
    assert setup.pass_available() is False


def test_render_env_file_skips_empty():
    out = setup.render_env_file({
        "HYPERTRADE_ENVIRONMENT": "test",
        "HYPERTRADE_MASTER_ADDR": "0xabc",
        "HYPERTRADE_SUBACCOUNT_ADDR": "",
    })
    assert "HYPERTRADE_ENVIRONMENT=test" in out
    assert "HYPERTRADE_MASTER_ADDR=0xabc" in out
    assert "SUBACCOUNT" not in out
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3.11 -m pytest tests/test_setup.py -p no:warnings -q`
Expected: FAIL (`ModuleNotFoundError: hypertrade.setup`).

- [ ] **Step 3: Implement the pure helpers** — create `hypertrade/setup.py`:

```python
"""Interactive first-run setup: collect the minimum config needed to start the
daemon and persist it to `pass` (if available) or a .env file.

Pure helpers (no terminal I/O) live at the top so they can be unit-tested; the
interactive run() shell is added in Task 2.
"""

import os
import re
import shutil
from pathlib import Path

ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
PRIVKEY_RE = re.compile(r"^(0x)?[0-9a-fA-F]{64}$")
IPV4_RE = re.compile(r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$")

# Env keys required before the daemon can start (auth handled separately).
REQUIRED_KEYS = (
    "HYPERTRADE_ENVIRONMENT",
    "HYPERTRADE_MASTER_ADDR",
    "HYPERTRADE_API_WALLET_PRIV",
)


def validate_environment(value: str) -> bool:
    return value.strip() in ("prod", "test")


def validate_address(value: str) -> bool:
    return bool(ADDRESS_RE.match(value.strip()))


def validate_privkey(value: str) -> bool:
    return bool(PRIVKEY_RE.match(value.strip()))


def validate_ipv4(value: str) -> bool:
    match = IPV4_RE.match(value.strip())
    return bool(match) and all(0 <= int(octet) <= 255 for octet in match.groups())


def missing_values(resolved: dict) -> list:
    """Return the REQUIRED_KEYS whose value in `resolved` is missing/empty."""
    return [k for k in REQUIRED_KEYS if not str(resolved.get(k) or "").strip()]


def pass_available() -> bool:
    """True if the `pass` CLI exists and a store is initialized."""
    if shutil.which("pass") is None:
        return False
    return (Path(os.path.expanduser("~/.password-store")) / ".gpg-id").exists()


def pass_key(environment: str, name: str) -> str:
    """Map (environment, logical name) to the pass entry path the launchers read."""
    prefix = "hypertrade" if environment == "prod" else "hypertrade_test"
    return f"{prefix}/{name}"


def render_env_file(values: dict) -> str:
    """Render HYPERTRADE_* lines for a .env from a {KEY: value} dict (skips empty)."""
    return "".join(f"{k}={v}\n" for k, v in values.items() if str(v).strip())
```

- [ ] **Step 4: Run to verify they pass**

Run: `python3.11 -m pytest tests/test_setup.py -p no:warnings -q`
Expected: PASS (8 tests).

- [ ] **Step 5: Commit**

```bash
git add hypertrade/setup.py tests/test_setup.py
git commit -m "feat(setup): pure validators and backend helpers"
```

---

### Task 2: Interactive `run()` + persistence + entry point

**Files:**
- Modify: `hypertrade/setup.py` (append the interactive shell + `__main__`)
- Test: `tests/test_setup.py`

**Interfaces:**
- Consumes: all Task 1 helpers.
- Produces: `prompt_until_valid(prompt, validator, *, hidden=False, allow_empty=False, reader=None)->str`, `write_env_file(values: dict, path=".env")->None`, `pass_insert(key, value, *, runner=subprocess.run)->None`, `collect(reader=None)->dict` (returns `{"environment","master_addr","api_wallet_priv","subaccount_addr","env_values":dict,"secrets":dict}`), `persist(collected, *, runner=subprocess.run, reader=None, env_path=".env")->None`, `run(reader=None, runner=subprocess.run)->None`, and `python -m hypertrade.setup`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_setup.py`:

```python
import os


class _Reader:
    """Feeds scripted answers to prompt_until_valid / input()."""
    def __init__(self, answers):
        self.answers = list(answers)
    def __call__(self, _prompt=""):
        return self.answers.pop(0)


def test_prompt_reprompts_until_valid():
    reader = _Reader(["bad", "0x" + "a" * 40])
    assert setup.prompt_until_valid("addr: ", setup.validate_address, reader=reader) == "0x" + "a" * 40


def test_prompt_allows_empty_skip():
    reader = _Reader([""])
    assert setup.prompt_until_valid("opt: ", setup.validate_address, allow_empty=True, reader=reader) == ""


def test_write_env_file_is_0600_and_has_keys(tmp_path):
    p = tmp_path / ".env"
    setup.write_env_file({"HYPERTRADE_ENVIRONMENT": "test", "HYPERTRADE_MASTER_ADDR": "0xabc"}, str(p))
    assert (p.stat().st_mode & 0o777) == 0o600
    body = p.read_text()
    assert "HYPERTRADE_ENVIRONMENT=test" in body
    assert "HYPERTRADE_MASTER_ADDR=0xabc" in body


def test_write_env_file_merges_existing(tmp_path):
    p = tmp_path / ".env"
    p.write_text("HYPERTRADE_ENVIRONMENT=test\nHYPERTRADE_LISTEN_PORT=6487\n")
    setup.write_env_file({"HYPERTRADE_MASTER_ADDR": "0xabc"}, str(p))
    body = p.read_text()
    assert "HYPERTRADE_LISTEN_PORT=6487" in body   # preserved
    assert "HYPERTRADE_MASTER_ADDR=0xabc" in body   # added


def test_pass_insert_invokes_pass_cli():
    calls = []
    def runner(args, **kwargs):
        calls.append((args, kwargs.get("input")))
        class R: returncode = 0
        return R()
    setup.pass_insert("hypertrade/master_addr", "0xabc", runner=runner)
    assert calls[0][0][:3] == ["pass", "insert", "-m"]
    assert "hypertrade/master_addr" in calls[0][0]
    assert calls[0][1] == "0xabc\n"


def test_persist_uses_env_when_pass_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(setup, "pass_available", lambda: False)
    collected = {
        "environment": "test",
        "master_addr": "0x" + "a" * 40,
        "api_wallet_priv": "b" * 64,
        "subaccount_addr": "",
        "env_values": {"HYPERTRADE_ENVIRONMENT": "test"},
        "secrets": {"HYPERTRADE_MASTER_ADDR": "0x" + "a" * 40,
                    "HYPERTRADE_API_WALLET_PRIV": "b" * 64},
    }
    env_path = tmp_path / ".env"
    setup.persist(collected, reader=_Reader(["y"]), env_path=str(env_path))
    body = env_path.read_text()
    assert "HYPERTRADE_MASTER_ADDR=" in body
    assert "HYPERTRADE_API_WALLET_PRIV=" in body


def test_persist_uses_pass_when_available(tmp_path, monkeypatch):
    monkeypatch.setattr(setup, "pass_available", lambda: True)
    calls = []
    def runner(args, **kwargs):
        calls.append(args)
        class R: returncode = 0
        return R()
    collected = {
        "environment": "prod",
        "master_addr": "0x" + "a" * 40,
        "api_wallet_priv": "b" * 64,
        "subaccount_addr": "",
        "env_values": {},
        "secrets": {"HYPERTRADE_MASTER_ADDR": "0x" + "a" * 40,
                    "HYPERTRADE_API_WALLET_PRIV": "b" * 64},
    }
    setup.persist(collected, runner=runner, env_path=str(tmp_path / ".env"))
    inserted_keys = [a[-1] for a in calls]
    assert "hypertrade/master_addr" in inserted_keys
    assert "hypertrade/api_wallet_priv" in inserted_keys
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3.11 -m pytest tests/test_setup.py -p no:warnings -q`
Expected: FAIL (`prompt_until_valid`/`write_env_file`/`pass_insert`/`persist` not defined).

- [ ] **Step 3: Implement the interactive shell** — append to `hypertrade/setup.py`:

```python
import getpass
import subprocess

# logical name -> (env var, pass entry name) for the secret values
_SECRET_FIELDS = {
    "master_addr": ("HYPERTRADE_MASTER_ADDR", "master_addr"),
    "api_wallet_priv": ("HYPERTRADE_API_WALLET_PRIV", "api_wallet_priv"),
    "subaccount_addr": ("HYPERTRADE_SUBACCOUNT_ADDR", "subaccount_addr"),
    "webhook_secret": ("HYPERTRADE_WEBHOOK_SECRET", "webhook_secret"),
}


def prompt_until_valid(prompt, validator, *, hidden=False, allow_empty=False, reader=None):
    read = reader or (getpass.getpass if hidden else input)
    for _ in range(5):
        value = read(prompt).strip()
        if allow_empty and value == "":
            return ""
        if validator(value):
            return value
        print("  Invalid value, please try again.")
    raise SystemExit("Too many invalid attempts; aborting setup.")


def write_env_file(values: dict, path: str = ".env") -> None:
    """Write/update a .env (0600), merging `values` into any existing keys."""
    target = Path(path)
    existing = {}
    if target.exists():
        for line in target.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, _, val = line.partition("=")
                existing[key.strip()] = val
    existing.update({k: v for k, v in values.items() if str(v).strip()})
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(render_env_file(existing))
    os.chmod(tmp, 0o600)
    tmp.replace(target)


def pass_insert(key: str, value: str, *, runner=subprocess.run) -> None:
    """Store a secret in pass non-interactively (overwriting)."""
    runner(["pass", "insert", "-m", "-f", key], input=value + "\n", text=True, check=True)


def collect(reader=None) -> dict:
    """Prompt for the minimum config (only what is unset) and return it."""
    env = {k: os.environ.get(k, "") for k in REQUIRED_KEYS}
    environment = env["HYPERTRADE_ENVIRONMENT"] or prompt_until_valid(
        "Environment (prod/test): ", validate_environment, reader=reader)
    master = env["HYPERTRADE_MASTER_ADDR"] or prompt_until_valid(
        "Master address (0x...): ", validate_address, reader=reader)
    priv = env["HYPERTRADE_API_WALLET_PRIV"] or prompt_until_valid(
        "API wallet private key (hidden): ", validate_privkey, hidden=True, reader=reader)
    sub = prompt_until_valid(
        "Sub-account address (optional, Enter to skip): ",
        validate_address, allow_empty=True, reader=reader)

    secrets = {"HYPERTRADE_MASTER_ADDR": master, "HYPERTRADE_API_WALLET_PRIV": priv}
    if sub:
        secrets["HYPERTRADE_SUBACCOUNT_ADDR"] = sub
    env_values = {"HYPERTRADE_ENVIRONMENT": environment}

    method = ""
    while method not in ("s", "w"):
        method = (reader or input)("Auth method — [s]ecret or [w]hitelist: ").strip().lower()
    if method == "s":
        secret = prompt_until_valid(
            "Webhook shared secret (hidden): ", lambda v: bool(v), hidden=True, reader=reader)
        secrets["HYPERTRADE_WEBHOOK_SECRET"] = secret
    else:
        env_values["HYPERTRADE_IP_WHITELIST_ENABLED"] = "true"
        ips = prompt_until_valid(
            "First allowed IPv4: ", validate_ipv4, reader=reader)
        env_values["HYPERTRADE_TV_WEBHOOK_IPS"] = f'["{ips}"]'

    return {
        "environment": environment, "master_addr": master,
        "api_wallet_priv": priv, "subaccount_addr": sub,
        "env_values": env_values, "secrets": secrets,
    }


def persist(collected: dict, *, runner=subprocess.run, reader=None, env_path: str = ".env") -> None:
    """Persist collected values to pass (if available) or a .env fallback."""
    environment = collected["environment"]
    secrets = collected["secrets"]
    env_values = collected["env_values"]
    if pass_available():
        for env_key, value in secrets.items():
            name = next(n for _, (e, n) in _SECRET_FIELDS.items() if e == env_key)
            pass_insert(pass_key(environment, name), value, runner=runner)
        # non-secret flags (whitelist) still go to .env so Settings picks them up
        if env_values:
            write_env_file(env_values, env_path)
        print("Saved secrets to `pass`" + (" and flags to .env." if env_values else "."))
    else:
        print(
            "\n`pass` was not found. Recommended for secret storage:\n"
            "  Debian/Ubuntu:  sudo apt install pass\n"
            "  macOS:          brew install pass\n"
            "  then initialize: pass init <your-gpg-id>\n"
        )
        answer = (reader or input)("Continue with a .env file instead? [Y/n]: ").strip().lower()
        if answer in ("n", "no"):
            raise SystemExit("Install `pass` and re-run `python -m hypertrade.setup`.")
        write_env_file({**env_values, **secrets}, env_path)
        print(f"Wrote configuration to {env_path} (mode 0600).")


def run(reader=None, runner=subprocess.run) -> None:
    print("HyperTrade guided setup — fill in the missing configuration.\n")
    persist(collect(reader=reader), runner=runner, reader=reader)
    print("\nDone. You can now start the daemon.")


if __name__ == "__main__":
    run()
```

- [ ] **Step 4: Run to verify they pass**

Run: `python3.11 -m pytest tests/test_setup.py -p no:warnings -q`
Expected: PASS (all setup tests).

- [ ] **Step 5: Run the full suite (no regressions)**

Run: `python3.11 -m pytest -p no:warnings -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add hypertrade/setup.py tests/test_setup.py
git commit -m "feat(setup): interactive run() with pass/.env persistence"
```

---

### Task 3: Integration — banner, launchers, README

**Files:**
- Modify: `hypertrade/daemon.py` (the `_please_die_gracefully` banner string)
- Modify: `hypertrade-prod.sh`, `hypertrade-test.sh`, `hypertrade-prod.fish`, `hypertrade-test.fish`
- Modify: `README.md`

**Interfaces:**
- Consumes: `python -m hypertrade.setup` (Task 2).
- Produces: launchers run setup when config is missing; banner + README point to the command. No Python API.

- [ ] **Step 1: Point the daemon banner at the setup command** — in `hypertrade/daemon.py`, inside the `banner` string in `_please_die_gracefully`, change the closing line:

```python
        "The daemon will start automatically once these are set.\n"
        "\n"
        "Tip: run  python -m hypertrade.setup  for a guided, interactive setup.\n"
```

- [ ] **Step 2: Add the pre-flight guard to the bash launchers** — in `hypertrade-prod.sh`, insert ABOVE the first `export HYPERTRADE_MASTER_ADDR=...` line:

```bash
# Pre-flight: guided setup if required secrets are not in pass yet.
if ! pass show hypertrade/master_addr >/dev/null 2>&1; then
  HYPERTRADE_ENVIRONMENT=prod python -m hypertrade.setup || exit 1
fi
```

and ADD an auth export alongside the existing exports:

```bash
export HYPERTRADE_WEBHOOK_SECRET=$(pass show hypertrade/webhook_secret 2>/dev/null | head -n 1)
```

Do the same in `hypertrade-test.sh` but with the `hypertrade_test/` prefix and `HYPERTRADE_ENVIRONMENT=test`.

- [ ] **Step 3: Add the pre-flight guard to the fish launchers** — in `hypertrade-prod.fish`, insert ABOVE the first `set -x HYPERTRADE_MASTER_ADDR ...` line:

```fish
# Pre-flight: guided setup if required secrets are not in pass yet.
if not pass show hypertrade/master_addr >/dev/null 2>&1
  env HYPERTRADE_ENVIRONMENT=prod python -m hypertrade.setup; or exit 1
end
```

and add the auth export:

```fish
set -x HYPERTRADE_WEBHOOK_SECRET (pass show hypertrade/webhook_secret 2>/dev/null | head -n 1)
```

Do the same in `hypertrade-test.fish` with the `hypertrade_test/` prefix and `HYPERTRADE_ENVIRONMENT=test`.

- [ ] **Step 4: Document it in the README** — in `README.md`, add a "Guided setup" subsection under the install/run area:

```markdown
### Guided setup (recommended for first run)

Run an interactive wizard that collects the minimum config needed to start
and stores it for you:

```bash
python -m hypertrade.setup
```

It prompts only for what is missing — environment (`prod`/`test`), master
address, API wallet private key (hidden), an optional sub-account, and one
authentication method (a webhook secret or the IP whitelist). Secrets are
saved to [`pass`](https://www.passwordstore.org/) when it is installed;
otherwise the wizard recommends installing `pass` and falls back to writing a
`.env` file (mode `0600`). The `hypertrade-{prod,test}.{sh,fish}` launchers run
this automatically when required configuration is not yet present.
```

- [ ] **Step 5: Verify scripts parse and the suite is green**

Run:
```bash
bash -n hypertrade-prod.sh && bash -n hypertrade-test.sh && echo "bash OK"
fish -n hypertrade-prod.fish && fish -n hypertrade-test.fish && echo "fish OK" || echo "fish not installed (skip)"
python3.11 -m pytest -p no:warnings -q
```
Expected: `bash OK`; fish OK if fish is installed; full suite PASS.

- [ ] **Step 6: Commit**

```bash
git add hypertrade/daemon.py hypertrade-prod.sh hypertrade-test.sh hypertrade-prod.fish hypertrade-test.fish README.md
git commit -m "feat(setup): wire guided setup into launchers, banner, and README"
```

---

## Self-Review

**Spec coverage:**
- §3 prompted values (env/master/priv/sub/auth) → Task 2 `collect()`. ✅
- §4 backend detection + pass namespace + recommend-install + .env fallback → Task 1 `pass_available`/`pass_key`, Task 2 `persist()`. ✅
- §5 pure/I-O split, module + launchers + banner + README → Task 1 (pure), Task 2 (I/O + entry), Task 3 (integration). ✅
- §6 error handling (re-prompt, getpass, atomic 0600 .env, merge not truncate) → Task 2 `prompt_until_valid`/`write_env_file`. ✅
- §7 testing (validators, missing, pass detection, pass_key, render_env, run with injected input / mocked subprocess / pass-absent .env) → Tasks 1-2 tests. ✅

**Placeholder scan:** No TBD/TODO/"similar to"; every code step shows exact code. Task 3 bash/fish edits give the literal snippets and name the per-file prefix differences explicitly.

**Type consistency:** `REQUIRED_KEYS`, `pass_available`, `pass_key(environment, name)`, `render_env_file`, `prompt_until_valid`, `write_env_file`, `pass_insert(key, value, runner=)`, `collect()` dict shape (`environment`/`secrets`/`env_values`), and `persist(collected, runner=, reader=, env_path=)` are used identically across Tasks 1-2 and their tests.
