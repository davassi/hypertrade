from __future__ import annotations

import os
import time
import logging
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING, InvalidOperation
from enum import Enum
from typing import Literal, Any, Dict, Optional, Tuple

from eth_account import Account
from hyperliquid.exchange import Exchange
from .hyperliquid_data_client import HyperliquidDataClient

logger = logging.getLogger("uvicorn.error")

TIF = Literal["Gtc", "Ioc", "Alo"]

class OrderSide(Enum):
    BUY = "buy"
    SELL = "sell"

class PositionSide(Enum):
    LONG = "long"
    SHORT = "short"

    def opposite(self) -> "PositionSide":
        """Return the opposite position side."""
        return PositionSide.SHORT if self == PositionSide.LONG else PositionSide.LONG

class OrderStatus(Enum):
    RESTING = "resting"
    FILLED = "filled"
    UNKNOWN = "unknown"

class HyperliquidExecutionClient:
    """
    Clean, robust wrapper around Hyperliquid Exchange SDK.
    Designed for trading bots: fast market orders, safe limit orders, instant cancel-or-reverse.
    """

    def __init__(
        self,
        private_key: str,
        account_address: Optional[str] = None,
        vault_address: Optional[str] = None,
        base_url: str = "https://api.hyperliquid.xyz",
        default_premium_bps: float = 5.0,
    ):
        if not private_key:
            raise ValueError("private_key must be provided")
        
        pk = private_key if private_key.startswith("0x") else f"0x{private_key}"
        wallet = Account.from_key(pk)

        self.exchange = Exchange(
            wallet,
            base_url=base_url,
            vault_address=vault_address,
            account_address=account_address,
        )
        self.data = HyperliquidDataClient(account_address=account_address, base_url=base_url)
        self.default_premium_bps = float(os.environ.get("PREMIUM_BPS", default_premium_bps))

        logger.debug(
            "Initialized HyperliquidExecutionClient | Wallet: %s | Vault: %s",
            wallet.address,
            vault_address or "None",
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
        is_buy = side == PositionSide.LONG
        norm_price = self._normalize_price(symbol, price, is_buy=is_buy)

        res = self.exchange.order(
            symbol,          # ← positional: coin
            is_buy,          # ← positional
            size,            # ← positional: sz
            norm_price,      # ← positional: limit_px
            {"limit": {"tif": tif}},
            reduce_only,
            cloid,
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
        premium = premium_bps or self.default_premium_bps
        is_buy = side == PositionSide.LONG
        aggressive_px = self._aggressive_price_from_impact(symbol, is_buy=is_buy, premium_bps=premium)
        norm_px = self._normalize_price(symbol, aggressive_px, is_buy=is_buy)

        return self.exchange.order(
            symbol,
            is_buy,
            size,
            norm_px,
            {"limit": {"tif": "Ioc"}},
            reduce_only,
            cloid,
        )
    
    def market_close(
        self,
        symbol: str,
        cloid: Optional[Cloid] = None,
    ) -> Dict[str, Any]:
        """Close entire position in one official call"""
        
        return self.exchange.market_close(symbol, None)

    def close_position(
        self,
        symbol: str,
        side: PositionSide,
        size: float,
        premium_bps: Optional[float] = None,
        cloid: Optional[str] = None,
        max_retries: int = 1,
    ) -> Dict[str, Any]:
        """
        Close an existing position instantly using reduce-only market-like order.
        Retries once with 3x premium if first attempt fails to cross.
        """
        premium = premium_bps or self.default_premium_bps
        res = self.market_order(
            symbol=symbol,
            side=side.opposite(),  # to close LONG → sell, to close SHORT → buy
            size=size,
            premium_bps=premium,
            reduce_only=True,
            cloid=cloid,
        )

        # Auto-retry on IOC failure
        if max_retries > 0:
            if self._was_ioc_rejected(res):
                print(f"IOC close failed for {symbol}, retrying with 3x premium...")
                time.sleep(2)
                return self.close_position(
                    symbol=symbol,
                    side=side,
                    size=size,
                    premium_bps=max(premium * 3, 50.0),
                    cloid=cloid,
                    max_retries=max_retries - 1,
                )
        return res

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
            print(f"Cancelling resting order {oid} on {symbol}")
            return self.exchange.cancel(symbol, oid)
        elif status == OrderStatus.FILLED:
            print(f"Reversing filled {position_side.value} position ({filled_size} {symbol})")
            return self.close_position(symbol, position_side, filled_size)
        else:
            raise ValueError(f"Cannot handle order status: {status}")

    # ===================================================================
    # Internal helpers
    # ===================================================================

    @staticmethod
    def _extract_oid_and_status(res: Dict[str, Any]) -> Tuple[int, OrderStatus]:
        try:
            statuses = res["response"]["data"]["statuses"]
            for s in statuses:
                if "resting" in s:
                    return int(s["resting"]["oid"]), OrderStatus.RESTING
                if "filled" in s:
                    return int(s["filled"]["oid"]), OrderStatus.FILLED
            # Fallback: check for error
            if statuses and "error" in statuses[0]:
                raise ValueError(statuses[0]["error"])
            raise ValueError("No resting/filled status found")
        except Exception as e:
            raise Exception(f"Failed to parse order response: {res}") from e

    @staticmethod
    def _was_ioc_rejected(res: Dict[str, Any]) -> bool:
        try:
            status = res["response"]["data"]["statuses"][0]
            return "error" in status and "could not immediately match" in status["error"]
        except Exception:
            return False

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
        except Exception:
            return Decimal("0.001")

    def _normalize_price(self, symbol: str, price: float, is_buy: bool) -> float:
        if price <= 0:
            raise ValueError(f"Invalid price: {price}")

        tick = self._get_tick_size(symbol)
        d = Decimal(str(price))
        rounding = ROUND_CEILING if is_buy else ROUND_FLOOR
        normalized = d.quantize(tick, rounding=rounding)
        return float(normalized)
