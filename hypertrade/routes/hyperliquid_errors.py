"""Hyperliquid client error taxonomy and request-error translation.

The webhook retry loop (`_place_order_with_retry`) only understands these four
domain exceptions: validation errors are not retried, network errors are retried
with backoff (503), and API errors are retried. Raw `requests` transport
exceptions bypass that logic and surface as unhandled 500s, so every network
call must funnel its `requests` failures through `translate_request_errors`,
which maps them onto this taxonomy.
"""

from __future__ import annotations

import contextlib
from typing import Iterator

import requests


class HyperliquidError(Exception):
    """Base exception for Hyperliquid client errors."""


class HyperliquidNetworkError(HyperliquidError):
    """Raised for network-related errors (transient failures, can retry)."""


class HyperliquidValidationError(HyperliquidError):
    """Raised for validation errors (bad input, won't retry)."""


class HyperliquidAPIError(HyperliquidError):
    """Raised for API-level errors from Hyperliquid."""


@contextlib.contextmanager
def translate_request_errors(context: str) -> Iterator[None]:
    """Translate raw `requests` transport errors into the Hyperliquid taxonomy.

    Wrap any block that performs a `requests` network call (POST,
    `raise_for_status`, `.json()`) so transport failures reach the webhook retry
    loop as the domain exceptions it understands rather than as unhandled 500s.

    Mapping (specific subclasses are checked before the `RequestException` base —
    order matters, since `Timeout`, `ConnectionError` and `HTTPError` all derive
    from `RequestException`):

    - `requests.Timeout` / `requests.ConnectionError` -> `HyperliquidNetworkError`
      (transient, retryable).
    - `requests.HTTPError` -> `HyperliquidAPIError` (API-level, e.g. non-2xx).
    - any other `requests.RequestException` -> `HyperliquidNetworkError`
      (treated as a transient transport failure).

    Args:
        context: A short label for the failing call site, included in the raised
            exception message to aid log/trace correlation.

    Yields:
        None. The wrapped block runs inside the manager.

    Raises:
        HyperliquidNetworkError: On timeouts, connection errors, or any other
            transport-level `requests` failure.
        HyperliquidAPIError: On `requests.HTTPError` (an API-level error).
    """
    try:
        yield
    except (requests.Timeout, requests.ConnectionError) as exc:
        raise HyperliquidNetworkError(f"{context}: {exc}") from exc
    except requests.HTTPError as exc:
        raise HyperliquidAPIError(f"{context}: {exc}") from exc
    except requests.RequestException as exc:
        raise HyperliquidNetworkError(f"{context}: {exc}") from exc
