"""Tests for the Hyperliquid error taxonomy and the request-error translator.

`translate_request_errors` is the single chokepoint that turns raw `requests`
transport exceptions into the domain error taxonomy the webhook retry loop
understands, so a real network blip routes through the retry/backoff branch
instead of surfacing as an unhandled 500.
"""

from __future__ import annotations

import pathlib
import sys

import pytest
import requests

REPO_ROOT = str(pathlib.Path(__file__).resolve().parents[1])
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from hyperliquid.utils.error import ClientError, ServerError

from hypertrade.routes import hyperliquid_errors as errors
from hypertrade.routes import hyperliquid_service
from hypertrade.routes.hyperliquid_errors import (
    HyperliquidAPIError,
    HyperliquidNetworkError,
    HyperliquidValidationError,
    translate_request_errors,
)


def test_sdk_client_error_4xx_maps_to_validation_error():
    """An SDK ClientError (4xx exchange rejection: bad price, insufficient margin) is PERMANENT for
    that order → HyperliquidValidationError (no retry → the desk classifies it terminal and pauses
    fast). Previously it escaped the taxonomy → HTTP 500 → desk 'transient' → ~1h retry before pause."""
    with pytest.raises(HyperliquidValidationError):
        with translate_request_errors("order"):
            raise ClientError(422, "rejected", "invalid price", {})


def test_sdk_client_error_429_maps_to_network_error():
    """A 429 (rate-limited) ClientError is retryable → HyperliquidNetworkError, not terminal."""
    with pytest.raises(HyperliquidNetworkError):
        with translate_request_errors("order"):
            raise ClientError(429, "rate", "too many requests", {})


def test_sdk_server_error_maps_to_network_error():
    """An SDK ServerError (5xx server hiccup) is transient → HyperliquidNetworkError (retryable)."""
    with pytest.raises(HyperliquidNetworkError):
        with translate_request_errors("order"):
            raise ServerError(503, "service unavailable")


def test_connection_error_maps_to_network_error():
    """A dropped connection is transient → retryable HyperliquidNetworkError."""
    with pytest.raises(HyperliquidNetworkError):
        with translate_request_errors("ctx"):
            raise requests.ConnectionError("boom")


def test_timeout_maps_to_network_error():
    """A timeout is transient → retryable HyperliquidNetworkError."""
    with pytest.raises(HyperliquidNetworkError):
        with translate_request_errors("ctx"):
            raise requests.Timeout("slow")


def test_http_error_maps_to_api_error():
    """A non-2xx HTTP status is an API-level error → HyperliquidAPIError."""
    with pytest.raises(HyperliquidAPIError):
        with translate_request_errors("ctx"):
            raise requests.HTTPError("500 server error")


def test_generic_request_exception_maps_to_network_error():
    """Any other transport error falls back to HyperliquidNetworkError."""
    with pytest.raises(HyperliquidNetworkError):
        with translate_request_errors("ctx"):
            raise requests.RequestException("weird transport failure")


def test_http_error_is_not_swallowed_by_request_exception_base():
    """Ordering matters: HTTPError (a RequestException subclass) must map to the
    API error, not be caught first by the RequestException fallback branch."""
    with pytest.raises(HyperliquidAPIError):
        with translate_request_errors("ctx"):
            raise requests.HTTPError("nope")


def test_context_is_included_in_message():
    """The context string is preserved so logs/traces identify the call site."""
    with pytest.raises(HyperliquidNetworkError) as excinfo:
        with translate_request_errors("get_all_mids"):
            raise requests.ConnectionError("dropped")
    assert "get_all_mids" in str(excinfo.value)


def test_non_request_exceptions_pass_through_untouched():
    """Only `requests` transport errors are translated; domain errors pass through."""
    with pytest.raises(ValueError):
        with translate_request_errors("ctx"):
            raise ValueError("domain error stays a ValueError")


def test_service_reexports_taxonomy_for_backward_compat():
    """webhooks.py imports the taxonomy from hyperliquid_service; the names there
    must be the very same objects defined in hyperliquid_errors."""
    assert hyperliquid_service.HyperliquidNetworkError is errors.HyperliquidNetworkError
    assert hyperliquid_service.HyperliquidValidationError is errors.HyperliquidValidationError
    assert hyperliquid_service.HyperliquidAPIError is errors.HyperliquidAPIError
    assert hyperliquid_service.HyperliquidError is errors.HyperliquidError
