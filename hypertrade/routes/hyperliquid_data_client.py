from __future__ import annotations

import os
import time
from typing import Any, Dict, Tuple, Optional

import requests


class HyperliquidDataClient:
    """
    Ultra-fast, cached, thread-safe-free data client for Hyperliquid.
    No background threads → no more hanging on exit.
    Pure REST + smart caching.
    """

    def __init__(
        self,
        account_address: Optional[str] = None,
        base_url: str = "https://api.hyperliquid.xyz",
    ):
        self.info_url = base_url.rstrip("/") + "/info"
        self.account_address = account_address or os.environ.get("HYPERTRADE_MASTER_ADDR")

        # Caching for metaAndAssetCtxs (refreshed every 3 seconds max)
        self._meta_universe: list[Dict[str, Any]] = []
        self._asset_ctxs: list[Dict[str, Any]] = []
        self._last_fetch_time = 0.0
        self._cache_ttl = 2.8  # seconds (Hyperliquid updates ~every 1-3s)

        print(f"HyperliquidDataClient initialized | Base URL: {self.info_url}")

    # ===================================================================
    # Public API
    # ===================================================================

    def get_mid(self, symbol: str) -> float:
        return float(self._get_ctx(symbol)["midPx"])   # ← add float()

    def get_mark(self, symbol: str) -> float:
        return float(self._get_ctx(symbol)["markPx"])

    def get_index(self, symbol: str) -> float:
        return float(self._get_ctx(symbol)["oraclePx"])

    def get_funding(self, symbol: str) -> float:
        return float(self._get_ctx(symbol)["funding"])

    def get_open_interest(self, symbol: str) -> float:
        return float(self._get_ctx(symbol)["openInterest"])

    def get_day_notional_volume(self, symbol: str) -> float:
        return float(self._get_ctx(symbol)["dayNtlVlm"])

    def get_premium(self, symbol: str) -> float:
        return float(self._get_ctx(symbol)["premium"])

    def get_impact_prices(self, symbol: str) -> Tuple[float, float]:
        buy_px, sell_px = self._get_ctx(symbol)["impactPxs"]
        return float(buy_px), float(sell_px)

    def get_meta(self, symbol: str) -> Dict[str, Any]:
        self._refresh_cache_if_needed()
        idx = self._symbol_to_idx(symbol)
        return self._meta_universe[idx]

    def get_all_mids(self) -> Dict[str, float]:
        resp = requests.post(self.info_url, json={"type": "allMids"}, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        return {s: float(p) for s, p in data.items() if not s.startswith("@")}

    def get_available_balance(self, address: Optional[str] = None) -> float:
        addr = address or self.account_address
        if not addr:
            raise ValueError("Account address required (set HYPERTRADE_MASTER_ADDR or pass explicitly)")

        payload = {"type": "clearinghouseState", "user": addr}
        resp = requests.post(self.info_url, json=payload, timeout=5)
        resp.raise_for_status()
        data = resp.json()

        # Latest Hyperliquid format (Nov 2025+)
        if "withdrawable" in data:
            return float(data["withdrawable"])

        if "assetPositions" in data:
            usdc_pos = next((p for p in data["assetPositions"] if p["position"]["coin"] == "USDC"), None)
            if usdc_pos and "withdrawable" in usdc_pos["position"]:
                return float(usdc_pos["position"]["withdrawable"])

        margin = data.get("marginSummary", {})
        if "accountValue" in margin:
            # Fallback: accountValue minus some buffer (not perfect but safe)
            return float(margin["accountValue"]) * 0.95

        raise ValueError(f"Could not parse withdrawable balance for {addr}. Response: {data}")

    # ===================================================================
    # Internal: Smart caching
    # ===================================================================

    def _refresh_cache_if_needed(self) -> None:
        now = time.time()
        if now - self._last_fetch_time > self._cache_ttl:
            resp = requests.post(
                self.info_url,
                json={"type": "metaAndAssetCtxs"},
                timeout=5,
            )
            resp.raise_for_status()
            data = resp.json()

            self._meta_universe = data[0]["universe"]
            self._asset_ctxs = data[1]
            self._last_fetch_time = now

    def _get_ctx(self, symbol: str) -> Dict[str, Any]:
        self._refresh_cache_if_needed()
        idx = self._symbol_to_idx(symbol)
        return self._asset_ctxs[idx]

    def _symbol_to_idx(self, symbol: str) -> int:
        try:
            return next(i for i, asset in enumerate(self._meta_universe) if asset["name"] == symbol)
        except StopIteration:
            raise ValueError(f"Symbol '{symbol}' not found in Hyperliquid universe")