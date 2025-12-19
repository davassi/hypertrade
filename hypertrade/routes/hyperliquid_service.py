"""Lightweight Hyperliquid client abstraction used by webhook processing."""

import logging
import json

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from hypertrade.config import get_settings
from .tradingview_enums import Side, SignalType
from .hyperliquid_execution_client import HyperliquidExecutionClient, PositionSide

log = logging.getLogger("uvicorn.error")

@dataclass
class OrderRequest:
    """Request parameters for placing an order on Hyperliquid."""
    # pylint: disable=too-many-instance-attributes
    symbol: str
    side: Side
    signal: SignalType
    qty: Decimal
    price: Optional[Decimal] = None  # None -> market
    reduce_only: bool = False
    post_only: bool = False
    client_id: Optional[str] = None
    leverage: Optional[int] = None
    subaccount: Optional[str] = None

@dataclass
class OrderResult:
    """Response result returned from API clients."""
    # pylint: disable=too-many-instance-attributes
    status: str
    order_id: str
    symbol: str
    side: str
    qty: str
    price: Optional[str]
    reduce_only: bool
    post_only: bool
    client_id: Optional[str]

class HyperliquidError(Exception):
    """Base exception for Hyperliquid client errors."""

class HyperliquidNetworkError(HyperliquidError):
    """Raised for network-related errors (transient failures, can retry)."""

class HyperliquidValidationError(HyperliquidError):
    """Raised for validation errors (bad input, won't retry)."""

class HyperliquidAPIError(HyperliquidError):
    """Raised for API-level errors from Hyperliquid."""

class HyperliquidService:
    """Thin wrapper around Hyperliquid SDK/API.

    Currently operates in mock mode by default. Replace the internals of
    `_send_order` with real SDK calls when wiring up credentials.
    """

    def __init__(
        self,
        *,
        base_url: Optional[str] = None,
        master_addr: Optional[str] = None,
        api_wallet_priv: Optional[str] = None,
        subaccount_addr: Optional[str] = None,
    ):
        if base_url is None:
            settings = get_settings()
            self.base_url = settings.api_url
        else:
            self.base_url = base_url
        self.subaccount_addr = subaccount_addr

        log.debug(
            "Initializing HyperliquidService: master=%s subaccount=%s",
            master_addr, subaccount_addr or "(not set)"
        )

        self.client = HyperliquidExecutionClient(
            private_key=api_wallet_priv,
            account_address=master_addr,
            vault_address=subaccount_addr,
            base_url=self.base_url,
            default_premium_bps=5.0,
        )

        # Log which account will be used for trading
        if subaccount_addr:
            log.info("Trading account: SUBACCOUNT %s", subaccount_addr)
        else:
            log.info("Trading account: MASTER %s", master_addr)
    
    def place_order(self, request: OrderRequest) -> dict:
        """Validate and place an order."""

        # Basic validation/normalization
        if request.qty is None or Decimal(request.qty) <= 0:
            raise HyperliquidValidationError("Quantity must be > 0")

        if not request.symbol:
            raise HyperliquidValidationError("Symbol required")

        symbol = request.symbol.upper()

        # ===================================================================
        # Fresh data right before trading
        # ===================================================================
        log.debug("Fetching market data for symbol=%s", symbol)
        mid_price = (self.client.data.get_mid(symbol))
        mark_price = (self.client.data.get_mark(symbol))

        meta = self.client.data.get_meta(symbol)
        available = self.client.data.get_available_balance()

        max_leverage = meta.get("maxLeverage", 1)
        sz_decimals = int(meta.get("szDecimals", 3))

        log.info(
            "Market data retrieved: symbol=%s mid_price=%.6f mark_price=%.6f available_balance=%.2f max_leverage=%sx sz_decimals=%s",
            symbol, mid_price, mark_price, available, max_leverage, sz_decimals
        )

        # ===================================================================
        # Set the requested leverage
        # ===================================================================
        leverage = int(request.leverage or 1)
        log.debug("Requested leverage: symbol=%s requested=%sx max_allowed=%sx", symbol, leverage, max_leverage)

        if leverage < 1 or leverage > max_leverage:
            log.warning("Leverage validation failed: symbol=%s requested=%sx max_allowed=%sx", symbol, leverage, max_leverage)
            raise HyperliquidValidationError(f"Requested leverage {leverage}x is out of bounds (1–{max_leverage}x)")

        # Update leverage first as shown in the SDK examples
        log.debug("Updating leverage on exchange: symbol=%s leverage=%sx", symbol, leverage)
        leverage_response = self.client.update_leverage(leverage, symbol)

        if leverage_response.get('status') != 'ok':
            log.warning("Leverage update may have failed: symbol=%s response=%s", symbol, json.dumps(leverage_response, indent=2))
        else:
            log.debug("Leverage updated successfully: symbol=%s leverage=%sx", symbol, leverage)

        # ===================================================================
        # Safe position sizing (Set up max around 85% of available, never 100%)
        # ===================================================================
        # Round to asset's size decimals (critical!)

        size = round(request.qty * leverage, sz_decimals)
        log.debug("Position size calculated: contracts=%s leverage=%sx size=%s sz_decimals=%s", request.qty, leverage, size, sz_decimals)

        if size <= 0:
            log.warning("Position size invalid: size=%s contracts=%s leverage=%s", size, request.qty, leverage)
            raise HyperliquidValidationError("Size too small or zero → nothing to trade.")

        log.info("Executing %s position: symbol=%s size=%s side=%s signal=%s", "CLOSE" if request.signal in {SignalType.CLOSE_LONG, SignalType.CLOSE_SHORT} else "OPEN/ADD", symbol, size, request.side, request.signal)

        # ===================================================================
        # Execute order with configured premium for aggressive fills
        # This ensures IOC orders cross the spread and execute immediately.
        # Premium is configurable via HYPERTRADE_MARKET_ORDER_PREMIUM_BPS
        # ===================================================================
        settings = get_settings()
        premium = settings.market_order_premium_bps
        log.debug("Using market order premium: %d bps (%.2f%%)", premium, premium / 100.0)

        if request.signal in {SignalType.CLOSE_LONG, SignalType.CLOSE_SHORT}:
            log.debug("Closing position: symbol=%s side=%s size=%s", symbol, request.signal, size)
            res = self.client.close_position(
                symbol=symbol,
                side=_signal_to_position_side(request.signal),
                size=size,
                premium_bps=premium,
            )
        else:
            log.debug("Opening/adding position: symbol=%s side=%s size=%s", symbol, request.side, size)
            res = self.client.market_order(
                symbol=symbol,
                side=_to_position_side(request.side),
                size=size,
                premium_bps=premium,
            )
        
        # Safe printing – handle both filled and error cases
        if res is None:
            log.error("Order execution failed: symbol=%s side=%s size=%s (no response from API)", symbol, request.side, size)
            raise HyperliquidAPIError("Order Creation did not work")
        else:
            status = res["response"]["data"]["statuses"][0]
            if "filled" in status:
                st = status["filled"]
                log.info("Order filled successfully: symbol=%s size=%s avg_price=%s total_sz=%s", symbol, size, st["avgPx"], st["totalSz"])
            else:
                log.info("Order submitted: symbol=%s status=%s", symbol, status)

        return res

def _to_position_side(side: Side) -> PositionSide:
    """Convert TradingView Side enum (buy/sell) into Hyperliquid PositionSide."""
    if side == Side.BUY:
        return PositionSide.LONG
    if side == Side.SELL:
        return PositionSide.SHORT
    raise HyperliquidError(f"Unsupported side: {side}")

def _signal_to_position_side(signal: SignalType) -> PositionSide:
    """Map closing signals to the position side being flattened."""
    if signal == SignalType.CLOSE_LONG:
        return PositionSide.LONG
    if signal == SignalType.CLOSE_SHORT:
        return PositionSide.SHORT
    raise HyperliquidError(f"Unsupported close signal: {signal}")
        
