from __future__ import annotations

import logging
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING
from enum import Enum
from typing import Literal, Any, Dict, Optional, Tuple

import requests
from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.utils.types import Cloid
from hypertrade.config import get_settings
from .hyperliquid_data_client import HyperliquidDataClient
from .hyperliquid_errors import translate_request_errors

log = logging.getLogger("uvicorn.error")

TIF = Literal["Gtc", "Ioc", "Alo"]

class OrderSide(Enum):
    """Order side enumeration."""
    BUY = "buy"
    SELL = "sell"

class PositionSide(Enum):
    """Position side enumeration."""
    LONG = "long"
    SHORT = "short"

    def opposite(self) -> "PositionSide":
        """Return the opposite position side."""
        return PositionSide.SHORT if self == PositionSide.LONG else PositionSide.LONG

class OrderStatus(Enum):
    """Order status enumeration."""
    RESTING = "resting"
    FILLED = "filled"
    UNKNOWN = "unknown"

class HyperliquidExecutionClient:
    """
    Wrapper around Hyperliquid Exchange SDK.
    Designed for trading bots: fast market orders, safe limit orders, instant cancel-or-reverse.
    """

    def __init__(
        self,
        private_key: str,
        account_address: Optional[str] = None,
        vault_address: Optional[str] = None,
        base_url: Optional[str] = None,
        default_premium_bps: float = 5.0,
    ):
        if not private_key:
            raise ValueError("private_key must be provided")

        if base_url is None:
            settings = get_settings()
            base_url = settings.api_url

        pk = private_key if private_key.startswith("0x") else f"0x{private_key}"
        wallet = Account.from_key(pk)
        
        if not vault_address:
            vault_address = None  # use main account if not provided

        self.exchange = Exchange(
            wallet,
            base_url=base_url,
            vault_address=vault_address,
            account_address=account_address,
        )
        self.data = HyperliquidDataClient(account_address=account_address, base_url=base_url)
        self.default_premium_bps = float(default_premium_bps)
        # Retained for the cloid idempotency query (find_order_by_cloid): the
        # order-status lookup must be keyed by the account the order was placed on.
        self.account_address = account_address
        self.vault_address = vault_address
        self.info_url = base_url.rstrip("/") + "/info"

        log.debug(
            "Initialized HyperliquidExecutionClient | Wallet: %s | Vault: %s | Premium: %.1f bps",
            wallet.address,
            vault_address or "None",
            self.default_premium_bps,
        )

    # ===================================================================
    # Public: High-level order placement
    # ===================================================================

    def limit_order(
        self,
        symbol: str,
        side: PositionSide,
        size: float,
        price: float,
        tif: TIF = "Gtc",
        reduce_only: bool = False,
        cloid: Optional[str] = None,
    ) -> Tuple[int, OrderStatus]:
        """Place a limit order and return (order_id, status)."""
        is_buy = side == PositionSide.LONG
        norm_price = self._normalize_price(symbol, price, is_buy=is_buy)

        with translate_request_errors("limit_order"):
            res = self.exchange.order(
                symbol,          # ← positional: coin
                is_buy,          # ← positional
                size,            # ← positional: sz
                norm_price,      # ← positional: limit_px
                {"limit": {"tif": tif}},
                reduce_only,
                self._to_cloid(cloid),
            )
        return self._extract_oid_and_status(res)

    def market_order(
        self,
        symbol: str,
        side: PositionSide,
        size: float,
        premium_bps: Optional[float] = None,
        reduce_only: bool = False,
        cloid: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Place a market-like order using IOC with price impact premium."""
        
        size = float(size)  # defensive: the SDK wire-encoder rejects Decimal sizes
        premium = premium_bps or self.default_premium_bps
        is_buy = side == PositionSide.LONG
        aggressive_px = self._aggressive_price_from_impact(symbol, is_buy=is_buy, premium_bps=premium)
        norm_px = self._normalize_price(symbol, aggressive_px, is_buy=is_buy)

        with translate_request_errors("market_order"):
            return self.exchange.order(
                symbol,
                is_buy,
                size,
                norm_px,
                {"limit": {"tif": "Ioc"}},
                reduce_only,
                self._to_cloid(cloid),
            )

    def market_close(
        self,
        symbol: str,
    ) -> Dict[str, Any]:
        """Close entire position in one official call"""

        with translate_request_errors("market_close"):
            return self.exchange.market_close(symbol, None)

    def close_position(
        self,
        symbol: str,
        side: PositionSide,
        size: float,
        premium_bps: Optional[float] = None,
        cloid: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Close a position with a single reduce-only IOC market-like order.

        Does NOT escalate the premium on a non-crossing IOC: aggressive-close /
        slippage-tolerance policy belongs to the strategy bot, not this thin
        executor (TD-17). A non-fill response is returned to the caller as-is.
        (The previous nested 3x-premium retry also reused the same cloid, which
        conflicted with cloid idempotency — TD-1.)
        """
        premium = premium_bps or self.default_premium_bps
        return self.market_order(
            symbol=symbol,
            side=side.opposite(),  # to close LONG → sell, to close SHORT → buy
            size=size,
            premium_bps=premium,
            reduce_only=True,
            cloid=cloid,
        )

    def cancel_or_reverse(
        self,
        symbol: str,
        oid: int,
        status: OrderStatus,
        position_side: PositionSide,
        filled_size: float,
    ) -> Dict[str, Any]:
        """
        One-liner: cancel a resting order OR reverse a filled one.
        """
        if status == OrderStatus.RESTING:
            log.info("Cancelling resting order %s on %s", oid, symbol)
            with translate_request_errors("cancel"):
                return self.exchange.cancel(symbol, oid)
        
        elif status == OrderStatus.FILLED:
            log.info(
                "Reversing filled %s position (%s %s)",
                position_side.value,
                filled_size,
                symbol,
            )
            return self.close_position(symbol, position_side, filled_size)
        
        else:
            raise ValueError(f"Cannot handle order status: {status}")
        
    def update_leverage(self, leverage: int, symbol: str) -> dict:
        """Update leverage for a given symbol."""
        with translate_request_errors("update_leverage"):
            return self.exchange.update_leverage(leverage, symbol)

    def find_order_by_cloid(self, cloid: str, user: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Query the exchange for an order by client order id (cloid).

        Mirrors the SDK's ``Info.query_order_by_cloid``: it POSTs
        ``{"type": "orderStatus", "user": <user>, "oid": <cloid raw str>}`` to the
        ``/info`` endpoint. Hyperliquid replies with ``{"status": "order", ...}``
        when the order exists and ``{"status": "unknownOid"}`` when it does not.

        Args:
            cloid: The "0x"+32-hex client order id string the order was submitted
                with.
            user: The account the order was placed on. Defaults to the vault
                address if configured, else the master account address.

        Returns:
            The full response payload when the order exists, else None when the
            exchange reports it is unknown.

        Raises:
            HyperliquidNetworkError / HyperliquidAPIError: on transport failure.
                Callers MUST treat this as "cannot confirm" and not resubmit.
        """
        # Validate/normalize the cloid via the SDK's Cloid so we POST exactly the
        # raw form the exchange indexes by (and reject a malformed cloid early).
        raw_cloid = Cloid.from_str(cloid).to_raw()
        lookup_user = user or self.vault_address or self.account_address
        payload = {"type": "orderStatus", "user": lookup_user, "oid": raw_cloid}

        with translate_request_errors("find_order_by_cloid"):
            resp = requests.post(self.info_url, json=payload, timeout=5)
            resp.raise_for_status()
            data = resp.json()

        # "unknownOid" (or any non-"order" status) means the order never landed.
        if isinstance(data, dict) and data.get("status") == "order":
            return data
        return None

    # ===================================================================
    # Internal helpers
    # ===================================================================

    @staticmethod
    def _to_cloid(cloid: Optional[str]) -> Optional[Cloid]:
        """Convert a cloid string into the SDK's Cloid object (None passes through).

        The SDK's ``exchange.order`` expects a ``Cloid`` instance, not a raw
        string; passing the raw string silently produces an invalid order wire.
        """
        if not cloid:
            return None
        return Cloid.from_str(cloid)

    @staticmethod
    def _extract_oid_and_status(res: Dict[str, Any]) -> Tuple[int, OrderStatus]:
        try:
            statuses = res["response"]["data"]["statuses"]
        
            for s in statuses:
                if OrderStatus.RESTING.value in s:
                    rest = s[OrderStatus.RESTING.value]
                    return int(rest["oid"]), OrderStatus.RESTING
                if OrderStatus.FILLED.value in s:
                    filled = s[OrderStatus.FILLED.value]
                    return int(filled["oid"]), OrderStatus.FILLED
        
            # Fallback: check for error
            if statuses and "error" in statuses[0]:
                raise ValueError(statuses[0]["error"])
            
            # No known status found
            raise ValueError("No resting/filled status found")
        
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Failed to parse order response: {res}") from exc

    def _aggressive_price_from_impact(self, symbol: str, is_buy: bool, premium_bps: float) -> float:
        buy_impact, sell_impact = self.data.get_impact_prices(symbol)
        factor = premium_bps / 10_000.0
        price = buy_impact if is_buy else sell_impact
        return price * (1.0 + factor) if is_buy else price * (1.0 - factor)

    def _get_tick_size(self, symbol: str) -> Decimal:
        """Return tick size (e.g., 0.001 for 3 decimals)"""
        try:
            meta = self.data.get_meta(symbol)
            decimals = int(meta.get("szDecimals", 3))  # fallback to 3
            return Decimal("1") / Decimal("10") ** decimals
        except (TypeError, ValueError):
            return Decimal("0.001")

    def _normalize_price(self, symbol: str, price: float, is_buy: bool) -> float:
        if price <= 0:
            raise ValueError(f"Invalid price: {price}")

        # Hyperliquid perp prices: at most 5 significant figures AND at most
        # (6 - szDecimals) decimal places, else the exchange rejects with
        # "Order has invalid price." NB szDecimals is a SIZE precision, NOT a price
        # tick — the old tick = 10^-szDecimals produced over-precise prices that got
        # rejected. The aggressive premium is applied upstream, so nearest-rounding
        # onto the valid grid still crosses the spread for an IOC.
        sz_decimals = int(self.data.get_meta(symbol).get("szDecimals", 3))
        max_decimals = max(6 - sz_decimals, 0)
        return round(float(f"{price:.5g}"), max_decimals)
