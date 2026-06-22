"""Hyperliquid REST data client with per-instance (request-scoped) memoization
of the meta/asset-ctx snapshot.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Tuple, Optional

import requests
from hypertrade.config import get_settings
from .hyperliquid_errors import translate_request_errors

log = logging.getLogger("uvicorn.error")

class HyperliquidDataClient:
    """
    Lightweight REST-first data client for Hyperliquid.

    The meta universe and asset-context snapshot is memoized per instance: the
    first getter that needs it performs one `metaAndAssetCtxs` POST, and every
    subsequent getter on the same instance reuses that snapshot. Because the
    client is constructed per order / per request (see HyperliquidExecutionClient
    and routes/webhooks.py), this memo is request-scoped — there is intentionally
    no TTL, invalidation, or cross-instance cache.
    """

    def __init__(
        self,
        account_address: Optional[str] = None,
        base_url: Optional[str] = None,
    ):
        """Create the client with optional account and base URL overrides."""
        settings = get_settings()

        if base_url is None:
            base_url = settings.api_url

        self.info_url = base_url.rstrip("/") + "/info"
        self.account_address = account_address or settings.master_addr

        # Per-instance (request-scoped) memo of (universe, asset_ctxs). Populated
        # only on a successful metaAndAssetCtxs fetch; never invalidated.
        self._meta_cache: Optional[
            Tuple[list[Dict[str, Any]], list[Dict[str, Any]]]
        ] = None

        log.debug("HyperliquidDataClient initialized | Base URL: %s", self.info_url)

    # ===================================================================
    # Public API
    # ===================================================================

    def get_mid(self, symbol: str) -> float:
        """Return the mid price for a symbol."""
        return float(self._get_ctx(symbol)["midPx"])

    def get_mark(self, symbol: str) -> float:
        """Return the mark price for a symbol."""
        return float(self._get_ctx(symbol)["markPx"])

    def get_index(self, symbol: str) -> float:
        """Return the oracle index price for a symbol."""
        return float(self._get_ctx(symbol)["oraclePx"])

    def get_funding(self, symbol: str) -> float:
        """Return the current funding rate for a symbol."""
        return float(self._get_ctx(symbol)["funding"])

    def get_open_interest(self, symbol: str) -> float:
        """Return open interest for a symbol."""
        return float(self._get_ctx(symbol)["openInterest"])

    def get_day_notional_volume(self, symbol: str) -> float:
        """Return the 24h notional volume for a symbol."""
        return float(self._get_ctx(symbol)["dayNtlVlm"])

    def get_premium(self, symbol: str) -> float:
        """Return the current premium for a symbol."""
        return float(self._get_ctx(symbol)["premium"])

    def get_impact_prices(self, symbol: str) -> Tuple[float, float]:
        """Return the buy/sell impact prices for a symbol."""
        buy_px, sell_px = self._get_ctx(symbol)["impactPxs"]
        return float(buy_px), float(sell_px)

    def get_meta(self, symbol: str) -> Dict[str, Any]:
        """Return the metadata entry for a symbol."""
        universe, _ = self._fetch_meta_and_asset_ctxs()
        idx = self._symbol_to_idx(symbol, universe)
        return universe[idx]

    def get_all_mids(self) -> Dict[str, float]:
        """Return a mapping of non-index symbols to their mid prices."""
        data = self._post({"type": "allMids"})
        return {s: float(p) for s, p in data.items() if not s.startswith("@")}

    def get_available_balance(self, address: Optional[str] = None) -> float:
        """Return the withdrawable balance for the provided or default address."""
        addr = address or self.account_address
        if not addr:
            raise ValueError("Account address required")

        data = self._post({"type": "clearinghouseState", "user": addr})

        if "withdrawable" in data and data["withdrawable"] is not None:
            return float(data["withdrawable"])

        # This should never happen on mainnet today, but keep as safety net.
        raise ValueError(
            "Missing 'withdrawable' field in clearinghouseState response for "
            f"{addr}. Full response: {data}"
        )

    # ===================================================================
    # Internal helpers
    # ===================================================================

    def _post(self, payload: Dict[str, Any], timeout: int = 5) -> Any:
        """POST a JSON payload to the info endpoint and return the parsed body.

        Centralizes the POST + raise_for_status + .json() sequence shared by all
        data calls, translating raw `requests` transport failures into the
        Hyperliquid error taxonomy so they reach the webhook retry loop instead
        of surfacing as unhandled 500s.
        """
        with translate_request_errors(f"data_client POST {payload.get('type', '?')}"):
            resp = requests.post(self.info_url, json=payload, timeout=timeout)
            resp.raise_for_status()
            return resp.json()

    def _fetch_meta_and_asset_ctxs(self) -> Tuple[list[Dict[str, Any]], list[Dict[str, Any]]]:
        """Return the meta universe and asset contexts, memoized per instance.

        The first call performs one `metaAndAssetCtxs` POST and caches the result
        on this instance; subsequent calls reuse it at no network cost. The cache
        is only written on a successful fetch — if `_post` raises (transport or
        HTTP error), nothing is stored and the next call retries.
        """
        if self._meta_cache is None:
            data = self._post({"type": "metaAndAssetCtxs"})
            self._meta_cache = (data[0]["universe"], data[1])
        return self._meta_cache

    def _get_ctx(self, symbol: str) -> Dict[str, Any]:
        """Fetch the asset context for a symbol."""
        universe, asset_ctxs = self._fetch_meta_and_asset_ctxs()
        idx = self._symbol_to_idx(symbol, universe)
        return asset_ctxs[idx]

    @staticmethod
    def _symbol_to_idx(symbol: str, universe: list[Dict[str, Any]]) -> int:
        """Return the index of a symbol within the provided universe."""
        try:
            return next(i for i, asset in enumerate(universe) if asset["name"] == symbol)
        except StopIteration as exc:
            raise ValueError(f"Symbol '{symbol}' not found in Hyperliquid universe") from exc
