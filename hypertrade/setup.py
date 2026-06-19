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
