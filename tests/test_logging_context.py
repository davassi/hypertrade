"""Tests for format_log_context — the shared diagnostic-context formatter used to
build a uniform correlation suffix on failure log lines."""

from __future__ import annotations

import pathlib
import sys

REPO_ROOT = str(pathlib.Path(__file__).resolve().parents[1])
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from hypertrade.logging import format_log_context


def test_joins_key_values_in_order():
    assert format_log_context(symbol="SOL", side="buy", size=2.0) == "symbol=SOL side=buy size=2.0"


def test_skips_none_values():
    assert format_log_context(symbol="SOL", cloid=None, req_id="r-1") == "symbol=SOL req_id=r-1"


def test_empty_when_all_none():
    assert format_log_context(a=None, b=None) == ""


def test_tolerates_arbitrary_reprable_objects():
    assert format_log_context(payload={"k": 1}) == "payload={'k': 1}"
