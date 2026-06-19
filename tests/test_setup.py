"""Tests for the interactive setup wizard's pure logic and I/O shell."""

from __future__ import annotations

import os
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


def test_pass_insert_propagates_runner_error():
    import pytest
    def runner(args, **kwargs):
        raise RuntimeError("gpg locked")
    with pytest.raises(RuntimeError):
        setup.pass_insert("hypertrade/master_addr", "x", runner=runner)


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


def test_collect_normalizes_env_environment(monkeypatch):
    monkeypatch.setenv("HYPERTRADE_ENVIRONMENT", "PROD ")
    monkeypatch.setenv("HYPERTRADE_MASTER_ADDR", "0x" + "a" * 40)
    monkeypatch.setenv("HYPERTRADE_API_WALLET_PRIV", "b" * 64)
    # reader: subaccount skip, auth=secret, secret value
    result = setup.collect(reader=_Reader(["", "s", "shh"]))
    assert result["environment"] == "prod"


def test_collect_rejects_invalid_env_environment(monkeypatch):
    import pytest
    monkeypatch.setenv("HYPERTRADE_ENVIRONMENT", "staging")
    monkeypatch.setenv("HYPERTRADE_MASTER_ADDR", "0x" + "a" * 40)
    monkeypatch.setenv("HYPERTRADE_API_WALLET_PRIV", "b" * 64)
    with pytest.raises(SystemExit):
        setup.collect(reader=_Reader([""]))


def test_collect_whitelist_accepts_multiple_ips(monkeypatch):
    monkeypatch.setenv("HYPERTRADE_ENVIRONMENT", "test")
    monkeypatch.setenv("HYPERTRADE_MASTER_ADDR", "0x" + "a" * 40)
    monkeypatch.setenv("HYPERTRADE_API_WALLET_PRIV", "b" * 64)
    # reader: subaccount skip, auth=whitelist, ip1, ip2, blank to finish
    result = setup.collect(reader=_Reader(["", "w", "1.2.3.4", "5.6.7.8", ""]))
    assert result["env_values"]["HYPERTRADE_TV_WEBHOOK_IPS"] == '["1.2.3.4","5.6.7.8"]'
