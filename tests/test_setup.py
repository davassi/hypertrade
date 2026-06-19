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
