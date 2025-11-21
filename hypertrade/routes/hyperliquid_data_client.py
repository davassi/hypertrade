"""Hyperliquid REST data client without caching."""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Tuple, Optional

import requests

log = logging.getLogger("uvicorn.error")

class HyperliquidDataClient:
    """
    Lightweight REST-first data client for Hyperliquid.
    """

    def __init__(
        self,
        account_address: Optional[str] = None,
        base_url: str = "https://api.hyperliquid.xyz",
    ):
        """Create the client with optional account and base URL overrides."""
        self.info_url = base_url.rstrip("/") + "/info"
        self.account_address = account_address or os.environ.get("HYPERTRADE_MASTER_ADDR")

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
        resp = requests.post(self.info_url, json={"type": "allMids"}, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        return {s: float(p) for s, p in data.items() if not s.startswith("@")}

    def get_available_balance(self, address: Optional[str] = None) -> float:
        """Return the withdrawable balance for the provided or default address."""
        addr = address or self.account_address
        if not addr:
            raise ValueError("Account address required")

        payload = {"type": "clearinghouseState", "user": addr}
        resp = requests.post(self.info_url, json=payload, timeout=5)
        resp.raise_for_status()
        data = resp.json()

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

    def _fetch_meta_and_asset_ctxs(self) -> Tuple[list[Dict[str, Any]], list[Dict[str, Any]]]:
        """Fetch the latest meta universe and asset contexts."""
        resp = requests.post(
            self.info_url,
            json={"type": "metaAndAssetCtxs"},
            timeout=5,
        )
        resp.raise_for_status()
        data = resp.json()
        return data[0]["universe"], data[1]

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
