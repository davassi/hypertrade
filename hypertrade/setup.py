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
