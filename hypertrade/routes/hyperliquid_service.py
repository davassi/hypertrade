import os
import time
import logging
from dataclasses import dataclass, asdict
from decimal import Decimal
from typing import Optional

from .tradingview_enums import Side

logger = logging.getLogger("uvicorn.error")


@dataclass
class OrderRequest:
    symbol: str
    side: Side
    qty: Decimal
    price: Optional[Decimal] = None  # None -> market
    reduce_only: bool = False
    post_only: bool = False
    client_id: Optional[str] = None
    leverage: Optional[int] = None
    subaccount: Optional[str] = None


@dataclass
class OrderResult:
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
    pass


class HyperliquidClient:
    """Thin wrapper around Hyperliquid SDK/API.

    Currently operates in mock mode by default. Replace the internals of
    `_send_order` with real SDK calls when wiring up credentials.
    """

    def __init__(self, *, base_url: Optional[str] = None, mock: bool = True,
                 master_addr: Optional[str] = None, api_wallet_priv: Optional[str] = None,
                 subaccount_addr: Optional[str] = None):
        self.base_url = base_url or os.getenv("HL_BASE_URL", "https://api.hyperliquid.xyz")
        self.mock = mock
        self.master_addr = master_addr
        self.api_wallet_priv = api_wallet_priv
        self.subaccount_addr = subaccount_addr

    @classmethod
    def from_settings(cls, settings, *, mock: bool = True) -> "HyperliquidClient":
        priv = None
        try:
            priv = settings.api_wallet_priv.get_secret_value() if getattr(settings, "api_wallet_priv", None) else None
        except Exception:
            priv = None
        return cls(
            mock=mock,
            master_addr=getattr(settings, "master_addr", None),
            api_wallet_priv=priv,
            subaccount_addr=getattr(settings, "subaccount_addr", None),
        )

    def place_order(self, req: OrderRequest) -> dict:
        # Basic validation/normalization
        if req.qty is None or Decimal(req.qty) <= 0:
            raise HyperliquidError("qty must be > 0")
        symbol = (req.symbol or "").upper()
        if not symbol:
            raise HyperliquidError("symbol required")

        return self._send_order(req)

    # Backward compatible shim (deprecated)
    def place_order_simple(self, symbol: str, side: Side, qty: float, price: Optional[float], subaccount: str) -> dict:
        req = OrderRequest(
            symbol=symbol,
            side=side,
            qty=Decimal(str(qty)),
            price=Decimal(str(price)) if price is not None else None,
            subaccount=subaccount,
        )
        return self.place_order(req)

    def _send_order(self, req: OrderRequest) -> dict:
        if self.mock:
            # Simulate success
            oid = f"mock-{int(time.time()*1000)}"
            logger.info(
                "[MOCK] place_order: symbol=%s side=%s qty=%s price=%s reduceOnly=%s postOnly=%s sub=%s",
                req.symbol, req.side.value, str(req.qty), str(req.price) if req.price is not None else None,
                req.reduce_only, req.post_only, req.subaccount or self.subaccount_addr,
            )
            result = OrderResult(
                status="ok",
                order_id=oid,
                symbol=req.symbol,
                side=req.side.value,
                qty=str(req.qty),
                price=str(req.price) if req.price is not None else None,
                reduce_only=req.reduce_only,
                post_only=req.post_only,
                client_id=req.client_id,
            )
            return asdict(result)

        # Real implementation placeholder; integrate official SDK here
        # Example (pseudocode):
        # client = OfficialHLClient(priv_key=self.api_wallet_priv, master=self.master_addr)
        # resp = client.order(symbol=req.symbol, side=req.side.value, size=str(req.qty), price=str(req.price) if req.price else None, reduceOnly=req.reduce_only, postOnly=req.post_only, clientId=req.client_id, subaccount=req.subaccount or self.subaccount_addr)
        # return resp
        raise HyperliquidError("Real Hyperliquid SDK integration not implemented")
