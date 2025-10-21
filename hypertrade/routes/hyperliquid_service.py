import os
import time
import logging
from enum import Enum
from typing import Optional

from .tradingview_enums import Side

logger = logging.getLogger("uvicorn.error")

# Mock Hyperliquid SDK client
class HyperliquidClient:

    def __init__(self):
        self.api_key = os.getenv("HL_API_KEY", "")
        self.api_secret = os.getenv("HL_API_SECRET", "")
        self.base_url = os.getenv("HL_BASE_URL", "https://api.hyperliquid.xyz")

    def place_order(self, symbol: str, side: Side, qty: float, price: Optional[float], subaccount: str):
        # TODO: Replace with actual SDK call (market/limit). This is a mock.
        logger.info(f"[MOCK] place_order: symbol={symbol} side={side.value} qty={qty} price={price} sub={subaccount}")
        return {"status": "ok", "order_id": f"mock-{int(time.time()*1000)}", "side": side.value}
