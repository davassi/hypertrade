"""Contract tests for Settings env-var parsing of List[str] fields.

pydantic-settings' EnvSettingsSource JSON-decodes complex List[str] fields
(``tv_webhook_ips``, ``rate_limit_only_paths``, ``rate_limit_exclude_paths``,
``trusted_hosts``) *before* model validation runs, so these env vars MUST be
JSON arrays. A comma-separated value raises ``SettingsError`` at startup and
never reaches any field validator.

These tests lock that contract in CI so a comma-splitting parser is never
reintroduced expecting it to work for env input (see hypertrade/config.py).

``Settings(_env_file=None)`` isolates each case from the developer's local
``.env`` so results depend only on the env vars set via ``monkeypatch``.
"""

from __future__ import annotations

# pylint: disable=import-outside-toplevel

import pathlib
import sys

import pytest
from pydantic_settings import SettingsError

# Ensure this repo's package is importable (mirrors tests/test_webhook.py).
REPO_ROOT = str(pathlib.Path(__file__).resolve().parents[1])
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

DEFAULT_TV_WEBHOOK_IPS = [
    "52.89.214.238",
    "34.212.75.30",
    "54.218.53.128",
    "52.32.178.7",
]

# Every List[str] setting routed through the same EnvSettingsSource JSON decode,
# as (env var, attribute name). Kept together so the contract is verified for
# all of them, not just the user-facing IP whitelist.
LIST_FIELDS = [
    ("HYPERTRADE_TV_WEBHOOK_IPS", "tv_webhook_ips"),
    ("HYPERTRADE_TRUSTED_HOSTS", "trusted_hosts"),
    ("HYPERTRADE_RATE_LIMIT_ONLY_PATHS", "rate_limit_only_paths"),
    ("HYPERTRADE_RATE_LIMIT_EXCLUDE_PATHS", "rate_limit_exclude_paths"),
]
LIST_FIELD_IDS = [field for _, field in LIST_FIELDS]


def _set_required_env(monkeypatch) -> None:
    """Set the minimal required env for Settings, leaving list fields unset."""
    monkeypatch.setenv("HYPERTRADE_ENVIRONMENT", "test")
    monkeypatch.setenv("HYPERTRADE_MASTER_ADDR", "0xMASTER")
    monkeypatch.setenv("HYPERTRADE_API_WALLET_PRIV", "dummy-priv-key")
    monkeypatch.setenv("HYPERTRADE_WEBHOOK_SECRET", "secret")


def _make_settings():
    """Construct Settings isolated from any local .env file."""
    from hypertrade.config import Settings

    return Settings(_env_file=None)


@pytest.mark.parametrize("env_var, field_name", LIST_FIELDS, ids=LIST_FIELD_IDS)
def test_list_field_accepts_json_array(monkeypatch, env_var, field_name) -> None:
    """A JSON array env value is parsed into the expected list."""
    _set_required_env(monkeypatch)
    monkeypatch.setenv(env_var, '["alpha","beta"]')

    settings = _make_settings()

    assert getattr(settings, field_name) == ["alpha", "beta"]


@pytest.mark.parametrize("env_var, field_name", LIST_FIELDS, ids=LIST_FIELD_IDS)
def test_list_field_rejects_comma_separated(monkeypatch, env_var, field_name) -> None:
    """A comma-separated env value crashes startup with SettingsError."""
    _set_required_env(monkeypatch)
    monkeypatch.setenv(env_var, "alpha,beta")

    with pytest.raises(SettingsError) as exc_info:
        _make_settings()

    # Fail for the *right* reason: the offending list field, not an unrelated error.
    assert field_name in str(exc_info.value)


def test_tv_webhook_ips_rejects_bare_single_value(monkeypatch) -> None:
    """Even a single bare IP is not valid JSON and is rejected."""
    _set_required_env(monkeypatch)
    monkeypatch.setenv("HYPERTRADE_TV_WEBHOOK_IPS", "9.9.9.9")

    with pytest.raises(SettingsError) as exc_info:
        _make_settings()

    assert "tv_webhook_ips" in str(exc_info.value)


def test_tv_webhook_ips_defaults_when_unset(monkeypatch) -> None:
    """The hardcoded defaults apply when the env var is absent."""
    _set_required_env(monkeypatch)
    monkeypatch.delenv("HYPERTRADE_TV_WEBHOOK_IPS", raising=False)

    settings = _make_settings()

    assert settings.tv_webhook_ips == DEFAULT_TV_WEBHOOK_IPS
