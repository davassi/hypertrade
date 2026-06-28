"""Lightweight Hyperliquid client abstraction used by webhook processing."""

import logging
import json

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from hypertrade.config import get_settings
from .tradingview_enums import Side, SignalType
from .hyperliquid_execution_client import HyperliquidExecutionClient, PositionSide
from .hyperliquid_errors import (
    HyperliquidError,
    HyperliquidNetworkError,
    HyperliquidValidationError,
    HyperliquidAPIError,
    HyperliquidRejection,
)
from hypertrade.logging import format_log_context

log = logging.getLogger("uvicorn.error")

def _safe_json(obj: object) -> str:
    """json.dumps that never raises — falls back to repr for non-serialisable payloads.

    A logging path must never mask the original failure with a serialisation error.
    """
    try:
        return json.dumps(obj)
    except (TypeError, ValueError):
        return repr(obj)

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
    # Deterministic client order id (cloid). Derived from the request nonce/req_id
    # by the webhook layer so that a retry of the same request reuses the same
    # cloid, letting us query the exchange for it before resubmitting (TD-1).
    cloid: Optional[str] = None
    # Request-scoped correlation id (the webhook's req_id). Threaded onto the
    # request so failure logs in the service/webhook layers can be tied back to
    # the originating request. Distinct from cloid (the exchange-side order id).
    req_id: Optional[str] = None

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

class HyperliquidService:
    """Thin wrapper around the Hyperliquid SDK/API.

    Builds a `HyperliquidExecutionClient` against the live Hyperliquid API URL
    (derived from settings, or an explicit `base_url`) and places real orders
    through it via the Hyperliquid SDK. `place_order` fetches fresh market data,
    validates/sets leverage, sizes the order, and submits it to the exchange.
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

        # Premium is always passed explicitly from settings at place_order() call
        # time, so no default_premium_bps is set here.
        self.client = HyperliquidExecutionClient(
            private_key=api_wallet_priv,
            account_address=master_addr,
            vault_address=subaccount_addr,
            base_url=self.base_url,
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

        # Preserve the case of dex-qualified (HIP-3) coins like "xyz:NVDA": the dex
        # prefix is case-sensitive on Hyperliquid. Uppercasing it to "XYZ:NVDA" breaks
        # the dex meta lookup (metaAndAssetCtxs dex="XYZ" 500s) and name_to_asset.
        # Plain coins (BTC, eth) are still uppercased.
        symbol = request.symbol if ":" in request.symbol else request.symbol.upper()

        # ===================================================================
        # Fresh data right before trading
        # ===================================================================
        log.debug("Fetching market data for symbol=%s", symbol)
        mid_price = (self.client.data.get_mid(symbol))
        mark_price = (self.client.data.get_mark(symbol))

        meta = self.client.data.get_meta(symbol)

        max_leverage = meta.get("maxLeverage", 1)
        sz_decimals = int(meta.get("szDecimals", 3))

        log.info(
            "Market data retrieved: symbol=%s mid_price=%.6f mark_price=%.6f max_leverage=%sx sz_decimals=%s",
            symbol, mid_price, mark_price, max_leverage, sz_decimals
        )

        # ===================================================================
        # Set the requested leverage — OPENS ONLY.
        # A CLOSE/reduce order must never touch leverage: Hyperliquid can reject a
        # margin/leverage change while a position is OPEN, and a fatal abort here would
        # block the EXIT (the worst failure for live money). Closes leave it unchanged.
        # ===================================================================
        leverage = int(request.leverage or 1)
        is_close = bool(request.reduce_only) or request.signal in {SignalType.CLOSE_LONG, SignalType.CLOSE_SHORT}
        if is_close:
            log.debug("Close/reduce order — leaving leverage/margin unchanged: symbol=%s", symbol)
        else:
            log.debug("Requested leverage: symbol=%s requested=%sx max_allowed=%sx", symbol, leverage, max_leverage)
            if leverage < 1 or leverage > max_leverage:
                log.warning("Leverage validation failed: symbol=%s requested=%sx max_allowed=%sx", symbol, leverage, max_leverage)
                raise HyperliquidValidationError(f"Requested leverage {leverage}x is out of bounds (1–{max_leverage}x)")

            # HIP-3 dex/equity perps (e.g. "xyz:SMSN") are ISOLATED-ONLY — Hyperliquid
            # rejects a cross-margin leverage update on them ("Cross margin is not allowed
            # for this asset"). Main perps keep the cross default.
            is_cross = ":" not in symbol
            log.debug("Updating leverage on exchange: symbol=%s leverage=%sx is_cross=%s", symbol, leverage, is_cross)
            leverage_response = self.client.update_leverage(leverage, symbol, is_cross=is_cross)

            # A failed leverage/margin update is FATAL: placing the order anyway would open
            # at the exchange's DEFAULT leverage (the naked-10x incident). Abort the trade
            # so the strategy bot sees a failure and never holds a wrong-leverage position.
            if leverage_response.get('status') != 'ok':
                log.error(
                    "Leverage update REJECTED | %s | response=%s",
                    format_log_context(
                        symbol=symbol, requested_leverage=leverage,
                        max_leverage=max_leverage, is_cross=is_cross,
                        cloid=request.cloid, req_id=request.req_id,
                    ),
                    _safe_json(leverage_response),
                )
                raise HyperliquidValidationError(
                    f"Leverage/margin update rejected for {symbol} "
                    f"(requested {leverage}x, is_cross={is_cross}): {leverage_response.get('response')}"
                )
            log.debug("Leverage updated successfully: symbol=%s leverage=%sx is_cross=%s", symbol, leverage, is_cross)

        # ===================================================================
        # Position sizing
        # ===================================================================
        # `request.qty` is the position size (number of contracts) the strategy
        # wants to hold. Leverage is applied by the exchange via update_leverage
        # above — it must NOT also multiply the order size, or the resulting
        # exposure would be leverage^2. Round to the asset's size decimals.
        # NOTE: this executor applies no balance cap — constraining size to
        # available margin is the strategy bot's responsibility, not this thin
        # executor's.
        size = float(round(request.qty, sz_decimals))  # SDK wire-encoder wants float, not Decimal
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
                cloid=request.cloid,
            )
        else:
            log.debug("Opening/adding position: symbol=%s side=%s size=%s", symbol, request.side, size)
            res = self.client.market_order(
                symbol=symbol,
                side=_to_position_side(request.side),
                size=size,
                premium_bps=premium,
                reduce_only=request.reduce_only,
                cloid=request.cloid,
            )
        
        # Safe printing – handle both filled and error cases
        if res is None:
            log.error(
                "Order execution failed (no response from API) | %s",
                format_log_context(
                    symbol=symbol, side=request.side.value, size=size,
                    reduce_only=request.reduce_only,
                    cloid=request.cloid, req_id=request.req_id,
                ),
            )
            raise HyperliquidAPIError("Order Creation did not work")
        else:
            try:
                status = res["response"]["data"]["statuses"][0]
            except (KeyError, TypeError, IndexError) as exc:
                log.error(
                    "Unexpected order response shape | %s | response=%s",
                    format_log_context(
                        symbol=symbol, side=request.side.value, size=size,
                        cloid=request.cloid, req_id=request.req_id,
                    ),
                    _safe_json(res),
                )
                raise HyperliquidAPIError(f"Unexpected order response shape: {res}") from exc
            if "filled" in status:
                st = status["filled"]
                log.info("Order filled successfully: symbol=%s size=%s avg_price=%s total_sz=%s", symbol, size, st["avgPx"], st["totalSz"])
            elif "resting" in status:
                log.info("Order resting (not yet filled): symbol=%s status=%s", symbol, status)
            elif "error" in status:
                # The exchange accepted the request shape but rejected the order
                # (invalid price, insufficient margin, ...). Surface it instead of
                # reporting a phantom success, or the strategy bot desyncs from reality.
                log.error(
                    "Order rejected by exchange | %s | error=%s | response=%s",
                    format_log_context(
                        symbol=symbol, side=request.side.value, size=size,
                        reduce_only=request.reduce_only, leverage=leverage,
                        cloid=request.cloid, req_id=request.req_id,
                    ),
                    status["error"],
                    _safe_json(res),
                )
                # A recoverable exchange rejection (insufficient margin / could-not-match / bad price):
                # HyperliquidRejection gets ONE fresh-priced retry in the webhook loop, then surfaces
                # TERMINAL (HTTP 400 → fast pause) — NOT a 502 'transient' the desk would retry for ~1h.
                raise HyperliquidRejection(f"Exchange rejected order: {status['error']}")
            else:
                log.error(
                    "Unexpected order status | %s | status=%s",
                    format_log_context(
                        symbol=symbol, side=request.side.value, size=size,
                        cloid=request.cloid, req_id=request.req_id,
                    ),
                    _safe_json(status),
                )
                raise HyperliquidAPIError(f"Unexpected order status: {status}")

        return res

    def find_order_by_cloid(self, cloid: str) -> Optional[dict]:
        """Return the exchange's record of the order with ``cloid``, or None.

        Used by the webhook retry loop (TD-1) to check, BEFORE resubmitting, that
        a prior submission did not already land. The query runs against the
        trading account that the order was placed on — the subaccount/vault if one
        is configured, otherwise the master account — matching how the SDK's
        ``query_order_by_cloid`` keys lookups by ``user``.

        Returns:
            The order payload when the exchange reports the order exists, else
            None when it reports the order is unknown.

        Raises:
            HyperliquidNetworkError / HyperliquidAPIError: if the query transport
                fails. The caller MUST treat this as "cannot confirm" and refuse
                to resubmit, since a duplicate real order is the worse outcome.
        """
        user = self.subaccount_addr or self.client.account_address
        return self.client.find_order_by_cloid(cloid, user=user)

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
        
