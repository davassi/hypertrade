"""Lightweight Hyperliquid client abstraction used by webhook processing."""

import os
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from .tradingview_enums import Side, SignalType
from .hyperliquid_execution_client import HyperliquidExecutionClient

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
    """Raised for client-level errors or unimplemented operations."""

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
        self.base_url = base_url or os.getenv("HL_BASE_URL", "https://api.hyperliquid.xyz") 
        self.client = HyperliquidExecutionClient(
            private_key=api_wallet_priv,
            account_address=master_addr,
            vault_address=subaccount_addr,
            base_url=self.base_url,
            default_premium_bps=5.0,
        )
    
    def place_order(self, request: OrderRequest) -> dict:
        """Validate and place an order."""
        
        # Basic validation/normalization
        if request.qty is None or Decimal(request.qty) <= 0:
            raise HyperliquidError("Quantity must be > 0")
          
        if not request.symbol:
            raise HyperliquidError("Symbol required")
        
        symbol = request.symbol.upper()
        
        # ===================================================================
        # Fresh data right before trading
        # ===================================================================
        mid_price = (self.client.data.get_mid(symbol))
        mark_price = (self.client.data.get_mark(symbol))
        
        meta = self.client.data.get_meta(symbol)
        available = self.client.data.get_available_balance()

        log.info("%s Mid: %.6f | Mark: %.6f", symbol, mid_price, mark_price)
        log.info("Available balance: %.2f USDC", available)
        log.info(
            "Max leverage: %sx | Size decimals: %s",
            meta.get("maxLeverage", "N/A"),
            meta.get("szDecimals"),
        )

        # ===================================================================
        # Safe position sizing (Set up max around 85% of available, never 100%)
        # ===================================================================
        # Round to asset's size decimals (critical!)
        sz_decimals = int(meta.get("szDecimals", 3))
        size = round(request.qty, sz_decimals)
        
        if size <= 0:
            log.info("Size too small or zero → nothing to trade.")
            return
            
        log.info("→ [%s POSITION] %s %s (impact + IOC – guaranteed fill)", request.side, size, symbol)
        
        if request.signal in {SignalType.CLOSE_LONG, SignalType.CLOSE_SHORT}:
            log.info("   Closing position only.")
            #res = self.client.close_position(symbol=symbol) TODO
        else :
            log.info("   Opening/adding position.")    
            res = self.client.market_order(
                symbol=symbol,
                side=request.side,
                size=size,
                premium_bps=80,   # 0.8% extra aggression – adjust 50–200 bps as needed
            )
        
        # Safe printing – handle both filled and error cases
        if res is None:
            log.error("  Position creation did not work.")
            raise HyperliquidError("Order Creation did not work")
        else:
            status = res["response"]["data"]["statuses"][0]
            if "filled" in status:
                st = status["filled"]
                log.info("   Position filled: %s @ %s", st["totalSz"], st["avgPx"])
            else:
                log.info("   Position Order result: %s", status)
                
        return res
        
 
        
