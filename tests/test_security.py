"""Security tests: client IP extraction and X-Forwarded-For handling.

The IP whitelist is one of the two webhook authentication methods, so the
client-IP extraction must not be spoofable via a client-supplied
``X-Forwarded-For`` header.
"""

from __future__ import annotations

import pathlib
import sys

REPO_ROOT = str(pathlib.Path(__file__).resolve().parents[1])
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from hypertrade.security import _extract_client_ip


class _FakeClient:
    def __init__(self, host: str) -> None:
        self.host = host


class _FakeRequest:
    """Minimal stand-in exposing the attributes _extract_client_ip reads."""

    def __init__(self, *, xff: str | None = None, peer: str | None = "203.0.113.9") -> None:
        headers: dict[str, str] = {}
        if xff is not None:
            headers["x-forwarded-for"] = xff
        self.headers = headers
        self.client = _FakeClient(peer) if peer else None


def test_xff_uses_rightmost_not_spoofable_leftmost() -> None:
    """An attacker who prepends a whitelisted IP must not be trusted.

    Each proxy appends the address it observed to the right, so the right-most
    entry (added by our trusted proxy) is the only non-client-controlled value.
    """
    req = _FakeRequest(xff="52.89.214.238, 203.0.113.9")  # spoofed, real
    assert _extract_client_ip(req, trust_forwarded_for=True) == "203.0.113.9"


def test_xff_single_value_returned_when_trusted() -> None:
    req = _FakeRequest(xff="198.51.100.7", peer="10.0.0.1")
    assert _extract_client_ip(req, trust_forwarded_for=True) == "198.51.100.7"


def test_xff_ignored_when_not_trusted() -> None:
    req = _FakeRequest(xff="52.89.214.238", peer="203.0.113.9")
    assert _extract_client_ip(req, trust_forwarded_for=False) == "203.0.113.9"


def test_falls_back_to_peer_without_xff() -> None:
    req = _FakeRequest(xff=None, peer="203.0.113.9")
    assert _extract_client_ip(req, trust_forwarded_for=True) == "203.0.113.9"


def test_returns_none_when_no_peer_and_no_xff() -> None:
    req = _FakeRequest(xff=None, peer=None)
    assert _extract_client_ip(req, trust_forwarded_for=True) is None


def test_trust_forwarded_for_defaults_to_false(monkeypatch) -> None:
    """Secure default: do not honor X-Forwarded-For unless explicitly enabled."""
    from hypertrade.config import Settings

    monkeypatch.setenv("HYPERTRADE_ENVIRONMENT", "test")
    monkeypatch.setenv("HYPERTRADE_MASTER_ADDR", "0xMASTER")
    monkeypatch.setenv("HYPERTRADE_API_WALLET_PRIV", "dummy-priv-key")
    monkeypatch.setenv("HYPERTRADE_WEBHOOK_SECRET", "secret")
    monkeypatch.delenv("HYPERTRADE_TRUST_FORWARDED_FOR", raising=False)

    assert Settings(_env_file=None).trust_forwarded_for is False
