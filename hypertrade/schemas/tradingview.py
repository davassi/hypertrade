from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional
from pydantic import BaseModel

class General(BaseModel):
    strategy: Optional[str] = None
    ticker: str
    exchange: str
    interval: str
    time: datetime
    timenow: datetime
    secret: Optional[str] = None
    leverage: Optional[str] = None

class SymbolData(BaseModel):
    open: Decimal
    close: Decimal
    high: Decimal
    low: Decimal
    volume: Decimal

class Currency(BaseModel):
    quote: str
    base: str

class Position(BaseModel):
    position_size: Decimal
    
class Order(BaseModel):
    action: str
    contracts: Decimal
    price: Decimal
    id: str
    comment: Optional[str] = None
    alert_message: Optional[str] = None

class Market(BaseModel):
    position: str
    position_size: Decimal
    previous_position: str
    previous_position_size: Decimal

class TradingViewWebhook(BaseModel):
    general: General
    symbol_data: SymbolData
    currency: Currency
    position: Optional[Position] = None
    order: Order
    market: Market
