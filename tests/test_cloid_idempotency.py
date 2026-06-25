"""Tests for TD-1: deterministic cloid + query-before-resubmit idempotency.

The webhook retry loop must never submit a SECOND real order when a retry is
triggered after the first submission may already have reached the exchange.
The fix derives a deterministic cloid from the request's nonce/req_id and, on
any retry, queries the exchange for that cloid BEFORE resubmitting:

  - order already landed  -> return it, do NOT resubmit
  - query itself fails    -> raise, do NOT resubmit (cannot confirm safely)
  - order confirmed absent -> resubmit
"""

from __future__ import annotations

import hashlib
import pathlib
import sys
from decimal import Decimal

import pytest

REPO_ROOT = str(pathlib.Path(__file__).resolve().parents[1])
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from hyperliquid.utils.types import Cloid

from hypertrade.routes.webhooks import _place_order_with_retry, _derive_cloid
from hypertrade.routes.hyperliquid_service import OrderRequest
from hypertrade.routes.hyperliquid_errors import (
    HyperliquidNetworkError,
    HyperliquidAPIError,
    HyperliquidValidationError,
    HyperliquidRejection,
)
from hypertrade.routes.tradingview_enums import Side, SignalType


async def test_order_rejection_retries_once_then_surfaces_terminal():
    """An exchange order rejection (margin / could-not-match / bad price / 4xx) gets exactly ONE
    fresh-priced retry, then is surfaced as-is (terminal — HyperliquidRejection ⊂ ValidationError →
    HTTP 400 → desk pauses/unwinds fast, never the ~1h transient-retry). Two submits total."""
    submits = {"n": 0}

    class RejectingClient:
        def place_order(self, req):
            submits["n"] += 1
            raise HyperliquidRejection("Exchange rejected order: could not immediately match")

        def find_order_by_cloid(self, cloid):
            return None  # the rejected order never landed → safe to resubmit on the retry

    cloid = _derive_cloid("nonce-reject")
    with pytest.raises(HyperliquidRejection):
        await _place_order_with_retry(RejectingClient(), _order_request(cloid=cloid), max_retries=2)
    assert submits["n"] == 2  # initial submit + exactly one retry, then terminal


def _order_request(cloid: str | None = None) -> OrderRequest:
    return OrderRequest(
        symbol="SOL",
        side=Side.BUY,
        signal=SignalType.OPEN_LONG,
        qty=Decimal("1"),
        price=Decimal("100"),
        cloid=cloid,
    )


# ===================================================================
# Determinism + Cloid validity
# ===================================================================

def test_derive_cloid_is_deterministic_for_same_seed():
    """Same seed must yield the same cloid string (stable across retries/resends)."""
    assert _derive_cloid("nonce-123") == _derive_cloid("nonce-123")


def test_derive_cloid_differs_for_different_seeds():
    assert _derive_cloid("nonce-123") != _derive_cloid("nonce-456")


def test_derive_cloid_is_valid_per_sdk():
    """The derived string must be accepted by the SDK's Cloid.from_str."""
    cloid = _derive_cloid("nonce-123")
    # Must not raise; must round-trip to the same raw value.
    parsed = Cloid.from_str(cloid)
    assert parsed.to_raw() == cloid
    assert cloid.startswith("0x")
    assert len(cloid) == 34  # "0x" + 32 hex chars (16 bytes)


def test_derive_cloid_matches_expected_formula():
    seed = "nonce-123"
    expected = "0x" + hashlib.sha256(seed.encode()).hexdigest()[:32]
    assert _derive_cloid(seed) == expected


# ===================================================================
# Cloid threading: the derived cloid reaches the order submission
# ===================================================================

async def test_cloid_threaded_to_order_submission():
    """The cloid on the OrderRequest must be the one place_order receives."""
    seen = {}

    class CapturingClient:
        def place_order(self, req):
            seen["cloid"] = req.cloid
            return {"orderId": "1"}

    cloid = _derive_cloid("nonce-abc")
    await _place_order_with_retry(CapturingClient(), _order_request(cloid=cloid))
    assert seen["cloid"] == cloid


# ===================================================================
# Retry, order already landed -> do NOT resubmit
# ===================================================================

async def test_retry_order_already_landed_does_not_resubmit():
    """If the first submit times out but the order actually landed, the retry must
    detect it via find_order_by_cloid and return WITHOUT a second submission."""
    submit_calls = {"n": 0}

    class LandedClient:
        def place_order(self, req):
            submit_calls["n"] += 1
            # Attempt 0 raises a transient error (reply timed out).
            raise HyperliquidNetworkError("timeout")

        def find_order_by_cloid(self, cloid):
            return {"order": {"order": {"oid": 42, "cloid": cloid}}, "status": "order"}

    cloid = _derive_cloid("nonce-landed")
    result = await _place_order_with_retry(
        LandedClient(), _order_request(cloid=cloid), max_retries=2
    )
    # Exactly one real submission happened.
    assert submit_calls["n"] == 1
    # Result references the already-placed order.
    assert result is not None


# ===================================================================
# Retry, order absent -> resubmit
# ===================================================================

async def test_retry_order_absent_resubmits():
    """If the exchange confirms the order is absent, the retry resubmits."""
    submit_calls = {"n": 0}

    class AbsentThenOkClient:
        def place_order(self, req):
            submit_calls["n"] += 1
            if submit_calls["n"] == 1:
                raise HyperliquidNetworkError("timeout")
            return {"orderId": "resubmitted"}

        def find_order_by_cloid(self, cloid):
            return None  # confirmed absent

    cloid = _derive_cloid("nonce-absent")
    result = await _place_order_with_retry(
        AbsentThenOkClient(), _order_request(cloid=cloid), max_retries=2
    )
    assert submit_calls["n"] == 2
    assert result == {"orderId": "resubmitted"}


# ===================================================================
# Retry, query itself fails -> raise, do NOT resubmit
# ===================================================================

async def test_retry_query_fails_raises_and_does_not_resubmit():
    """If find_order_by_cloid itself raises, we cannot confirm landing — the safe
    choice is to NOT resubmit and surface the error."""
    submit_calls = {"n": 0}

    class QueryFailsClient:
        def place_order(self, req):
            submit_calls["n"] += 1
            raise HyperliquidNetworkError("timeout")

        def find_order_by_cloid(self, cloid):
            raise HyperliquidNetworkError("query timeout")

    cloid = _derive_cloid("nonce-queryfail")
    with pytest.raises(HyperliquidNetworkError):
        await _place_order_with_retry(
            QueryFailsClient(), _order_request(cloid=cloid), max_retries=2
        )
    # Only the original attempt — no resubmission after an unconfirmable query.
    assert submit_calls["n"] == 1


async def test_retry_query_api_error_raises_and_does_not_resubmit():
    """An APIError from the query is equally unconfirmable -> no resubmit."""
    submit_calls = {"n": 0}

    class QueryApiErrorClient:
        def place_order(self, req):
            submit_calls["n"] += 1
            raise HyperliquidAPIError("api error")

        def find_order_by_cloid(self, cloid):
            raise HyperliquidAPIError("query api error")

    cloid = _derive_cloid("nonce-queryapifail")
    with pytest.raises(HyperliquidAPIError):
        await _place_order_with_retry(
            QueryApiErrorClient(), _order_request(cloid=cloid), max_retries=2
        )
    assert submit_calls["n"] == 1


# ===================================================================
# Validation errors are never retried (and never queried)
# ===================================================================

async def test_validation_error_not_retried_or_queried():
    submit_calls = {"n": 0}
    query_calls = {"n": 0}

    class ValidationClient:
        def place_order(self, req):
            submit_calls["n"] += 1
            raise HyperliquidValidationError("bad input")

        def find_order_by_cloid(self, cloid):
            query_calls["n"] += 1
            return None

    cloid = _derive_cloid("nonce-validation")
    with pytest.raises(HyperliquidValidationError):
        await _place_order_with_retry(
            ValidationClient(), _order_request(cloid=cloid), max_retries=2
        )
    assert submit_calls["n"] == 1
    assert query_calls["n"] == 0


# ===================================================================
# Defensive: no cloid -> falls back to plain retry behavior
# ===================================================================

async def test_no_cloid_falls_back_to_plain_retry():
    """If no cloid is present (shouldn't happen since req_id is always set), the
    loop falls back to the original retry-and-resubmit behavior."""
    submit_calls = {"n": 0}

    class NoCloidClient:
        def place_order(self, req):
            submit_calls["n"] += 1
            if submit_calls["n"] == 1:
                raise HyperliquidNetworkError("timeout")
            return {"orderId": "fallback"}

    result = await _place_order_with_retry(
        NoCloidClient(), _order_request(cloid=None), max_retries=2
    )
    assert submit_calls["n"] == 2
    assert result == {"orderId": "fallback"}
