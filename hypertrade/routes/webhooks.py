"""TradingView webhook endpoint: validate, parse and respond."""

import os
import logging
import hmac
import time
import json

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, BackgroundTasks
from fastapi.responses import JSONResponse
from jsonschema import (
    validate as jsonschema_validate,
    ValidationError as JSONSchemaValidationError,
)

from ..config import get_settings
from ..schemas.tradingview_schema import TRADINGVIEW_SCHEMA
from ..schemas.tradingview import TradingViewWebhook
from ..security import require_ip_whitelisted

from ..routes.tradingview_enums import SignalType, PositionType, OrderAction, Side

from .hyperliquid_service import (
    HyperliquidService,
    OrderRequest,
    HyperliquidValidationError,
    HyperliquidNetworkError,
    HyperliquidAPIError,
)

router = APIRouter(tags=["webhooks"])
history_router = APIRouter(tags=["history"])
log = logging.getLogger("uvicorn.error")

def _place_order_with_retry(client: HyperliquidService, order_request: OrderRequest, max_retries: int = 2) -> dict:
    """Attempt to place an order with exponential backoff for transient failures.

    Args:
        client: HyperliquidService instance
        order_request: Order details
        max_retries: Maximum number of retry attempts (excluding first attempt)

    Returns:
        Order result from API

    Raises:
        HyperliquidValidationError: For bad input (no retry)
        HyperliquidAPIError: For persistent API failures (retried)
        HyperliquidNetworkError: For network errors (retried)
    """
    for attempt in range(max_retries + 1):
        try:
            return client.place_order(order_request)
        except HyperliquidValidationError:
            # Don't retry validation errors - they're permanent
            log.warning("Order validation failed, not retrying: %s", order_request)
            raise
        except (HyperliquidNetworkError, HyperliquidAPIError) as e:
            if attempt < max_retries:
                wait_time = 2 ** attempt  # Exponential backoff: 1s, 2s, 4s...
                log.warning(
                    "Order placement attempt %d/%d failed, retrying in %ds: %s",
                    attempt + 1, max_retries + 1, wait_time, str(e)
                )
                time.sleep(wait_time)
            else:
                log.error("Order placement failed after %d attempts", max_retries + 1)
                raise

@router.post(
    "/webhook",
    dependencies=[Depends(require_ip_whitelisted(None))],
    summary="TradingView → Hyperliquid",
)
async def hypertrade_webhook(
    request: Request, 
    background_tasks: BackgroundTasks
) -> dict:
    """Main webhook endpoint: validates, parses, logs, and returns a summary."""
    start_time = time.perf_counter()
    
    # Let's start with our checks.

    # First: Require JSON content type.
    _require_json_content_type(request)
    
    # Second: Parse JSON body ourselves to avoid pre-validation errors on non-JSON content.
    raw = await _read_json_body(request)
    
    # Third: JSON Schema validation on raw payload
    _validate_schema(raw)
    
    # Fourth (Optional but recommended) secret enforcement: if env secret is set,
    # then the json payload requires to carry a matching general.secret.
    secret_enforcement(request, raw)

    payload = TradingViewWebhook.model_validate(raw)

    log.debug("Full webhook payload: %s", raw)

    signal = parse_signal(payload)
    log.debug(
        "Signal parsed: signal=%s action=%s current_position=%s previous_position=%s",
        signal.value,
        payload.order.action,
        payload.market.position,
        payload.market.previous_position,
    )
    side = signal_to_side(signal)
    log.debug("Position side resolved: signal=%s side=%s", signal.value, side.value if side else None)
    
    if not side or signal == SignalType.NO_ACTION:
        elapsed_ms = (time.perf_counter() - start_time) * 1000
        log.info("Webhook ignored in %.1f ms", elapsed_ms)
        return JSONResponse({
            "status": "ignored",
            "reason": "no_action",
            "signal": signal.value,
            "order_id": payload.order.id,
        })
    
    symbol = payload.currency.base.upper()
    try:
        contracts = float(payload.order.contracts)
        price = float(payload.order.price)    
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Invalid 'contracts' or 'price' value") from exc
        
    # Fifth: processing logic here (TODO: would be good to enqueue the job).
    nominal_quantity = float(contracts * price)
    
    log.info(
        "Order parameters: direction=%s symbol=%s interval=%s price=%.2f contracts=%s notional_qty=%.2f alert=%s",
        payload.order.action.upper(),
        payload.general.ticker.upper(),
        payload.general.interval,
        price,
        contracts,
        nominal_quantity,
        payload.order.alert_message or "(none)",
    )
    
    # ===================================================================
    # Config & Clients.
    # ===================================================================
    settings = get_settings()
    vault_address: Optional[str] = settings.subaccount_addr
    leverage = _parse_leverage(payload.general.leverage)

    # Validate that if subaccount is configured, it will be used
    if vault_address:
        log.debug("Subaccount configured: %s - order will trade on subaccount", vault_address)
    else:
        log.debug("No subaccount configured - order will trade on master account")

    client = HyperliquidService(
        base_url=settings.api_url,
        master_addr=settings.master_addr,
        api_wallet_priv=settings.api_wallet_priv.get_secret_value(),
        subaccount_addr=vault_address,
    )
    
    # Determine reduce_only flag based on signal type
    # REDUCE signals should only reduce existing positions, not open new ones
    reduce_only = signal in {SignalType.REDUCE_LONG, SignalType.REDUCE_SHORT}

    # Execute plugging into Hyperliquid SDK.
    order_request = OrderRequest(
        symbol=symbol,
        side=side,
        signal=signal,
        qty=contracts,
        price=price,
        reduce_only=reduce_only,
        post_only=False,
        client_id=None,
        leverage=leverage,
        subaccount=vault_address,
    )

    log.debug(
        "Order request prepared: symbol=%s side=%s qty=%s price=%s leverage=%sx reduce_only=%s",
        order_request.symbol,
        order_request.side.value,
        order_request.qty,
        order_request.price,
        order_request.leverage or 1,
        order_request.reduce_only,
    )

    # ===================================================================
    # EXECUTION: Place the order with retry logic.
    # ===================================================================

    req_id = getattr(request.state, "request_id", None)
    db = getattr(request.app.state, "db", None)

    try:
        log.info("Attempting to place order on Hyperliquid: symbol=%s side=%s", symbol, side.value)
        result = _place_order_with_retry(client, order_request, max_retries=2)
    except HyperliquidValidationError as e:
        log.warning("Order validation error: %s", e)
        if db and req_id:
            db.log_order(
                request_id=req_id,
                symbol=symbol,
                side=side.value,
                signal=signal.value,
                quantity=contracts,
                price=price,
                leverage=leverage,
                subaccount=vault_address,
                status="REJECTED",
                execution_ms=(time.perf_counter() - start_time) * 1000,
            )
            db.log_failure(
                request_id=req_id,
                error_type=e.__class__.__name__,
                error_message=str(e),
                attempt=1,
                retry_count=0,
            )
        raise HTTPException(status_code=400, detail=f"Invalid order: {e}") from e
    except HyperliquidNetworkError as e:
        log.error("Network error placing order (after retries): %s", e)
        if db and req_id:
            db.log_order(
                request_id=req_id,
                symbol=symbol,
                side=side.value,
                signal=signal.value,
                quantity=contracts,
                price=price,
                leverage=leverage,
                subaccount=vault_address,
                status="FAILED",
                execution_ms=(time.perf_counter() - start_time) * 1000,
            )
            db.log_failure(
                request_id=req_id,
                error_type=e.__class__.__name__,
                error_message=str(e),
                attempt=3,
                retry_count=2,
            )
        raise HTTPException(
            status_code=503,
            detail="Temporary service unavailable - order may have been placed, check manually"
        ) from e
    except HyperliquidAPIError as e:
        log.error("API error placing order (after retries): %s", e)
        if db and req_id:
            db.log_order(
                request_id=req_id,
                symbol=symbol,
                side=side.value,
                signal=signal.value,
                quantity=contracts,
                price=price,
                leverage=leverage,
                subaccount=vault_address,
                status="FAILED",
                execution_ms=(time.perf_counter() - start_time) * 1000,
            )
            db.log_failure(
                request_id=req_id,
                error_type=e.__class__.__name__,
                error_message=str(e),
                attempt=3,
                retry_count=2,
            )
        raise HTTPException(status_code=502, detail=f"Exchange error: {e}") from e

    log.info("Order placed successfully: %s", result)

    # Log successful order
    if db and req_id:
        db.log_order(
            request_id=req_id,
            symbol=symbol,
            side=side.value,
            signal=signal.value,
            quantity=contracts,
            price=price,
            leverage=leverage,
            subaccount=vault_address,
            status="PLACED",
            order_id=result.get("orderId"),
            avg_price=result.get("avgPx"),
            total_size=result.get("totalSz"),
            response_json=json.dumps(result) if result else None,
            execution_ms=(time.perf_counter() - start_time) * 1000,
        )
    
    # Finally: build a response.
    response = _build_response(payload, signal=signal, side=side, symbol=symbol)

    # Optional: shoot the response to Telegram (if configured).
    notifier = getattr(request.app.state, "telegram_notify", None)
    if notifier:
        req_id = getattr(request.state, "request_id", None)
        text = _format_telegram_message(
            payload=payload,
            symbol=symbol,
            signal=signal,
            side=side,
            req_id=req_id,
        )
        background_tasks.add_task(notifier, text)

    elapsed_ms = (time.perf_counter() - start_time) * 1000
    log.info("Webhook processed in %.1f ms", elapsed_ms)
    return response

# Enums and parsing logic
def _parse_leverage(raw: Optional[str]) -> Optional[int]:
    """Convert TradingView leverage string (optionally ending with 'x') into an int."""
    if raw is None:
        return None
    cleaned = str(raw).strip()
    if not cleaned:
        return None
    if cleaned.lower().endswith("x"):
        cleaned = cleaned[:-1].strip()
    if not cleaned:
        return None
    try:
        return int(cleaned)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid leverage value") from exc

def signal_to_side(signal: SignalType) -> Optional[Side]:
    """Map SignalType to order Side, or None if not actionable."""
    if signal in (
        SignalType.OPEN_LONG,
        SignalType.CLOSE_SHORT,
        SignalType.ADD_LONG,
        SignalType.REVERSE_TO_LONG,
        SignalType.REDUCE_SHORT,
    ):
        return Side.BUY
    if signal in (
        SignalType.OPEN_SHORT,
        SignalType.CLOSE_LONG,
        SignalType.ADD_SHORT,
        SignalType.REVERSE_TO_SHORT,
        SignalType.REDUCE_LONG,
    ):
        return Side.SELL
    return None

def parse_signal(payload: TradingViewWebhook) -> SignalType:
    """Return a normalized SignalType based on payload contents using Enums."""
    # pylint: disable=too-many-return-statements,too-many-branches
    # Coerce to enums safely
    try:
        current = PositionType(payload.market.position.lower())
    except (ValueError, AttributeError):
        current = PositionType.FLAT
    try:
        previous = (
            PositionType(payload.market.previous_position.lower())
            if payload.market.previous_position
            else PositionType.FLAT
        )
    except (ValueError, AttributeError):
        previous = PositionType.FLAT
    try:
        action = OrderAction(payload.order.action.lower())
    except (ValueError, AttributeError):
        return SignalType.NO_ACTION

    # Open/Close
    if previous == PositionType.FLAT and current == PositionType.LONG and action == OrderAction.BUY:
        return SignalType.OPEN_LONG
    if (
        previous == PositionType.LONG
        and current == PositionType.FLAT
        and action == OrderAction.SELL
    ):
        return SignalType.CLOSE_LONG
    if (
        previous == PositionType.FLAT
        and current == PositionType.SHORT
        and action == OrderAction.SELL
    ):
        return SignalType.OPEN_SHORT
    if (
        previous == PositionType.SHORT
        and current == PositionType.FLAT
        and action == OrderAction.BUY
    ):
        return SignalType.CLOSE_SHORT

    # Same-side changes (scale or partial closes)
    if previous == current and current in (PositionType.LONG, PositionType.SHORT):
        if current == PositionType.LONG and action == OrderAction.BUY:
            return SignalType.ADD_LONG
        if current == PositionType.LONG and action == OrderAction.SELL:
            return SignalType.REDUCE_LONG
        if current == PositionType.SHORT and action == OrderAction.SELL:
            return SignalType.ADD_SHORT
        if current == PositionType.SHORT and action == OrderAction.BUY:
            return SignalType.REDUCE_SHORT

    # Reversals
    if previous == PositionType.SHORT and current == PositionType.LONG:
        return SignalType.REVERSE_TO_LONG
    if previous == PositionType.LONG and current == PositionType.SHORT:
        return SignalType.REVERSE_TO_SHORT

    return SignalType.NO_ACTION


# Check the webhook secret if configured in environment
def secret_enforcement(request: Request, raw: dict) -> None:
    """Enforce optional shared secret in `general.secret`.

    If `HYPERTRADE_WEBHOOK_SECRET` (via settings) is set, the request JSON must
    contain a matching `general.secret`. Otherwise raise 401.
    """
    settings = request.app.state.settings
    env_secret = None

    if getattr(settings, "webhook_secret", None):
        env_secret = settings.webhook_secret.get_secret_value()

    if env_secret:
        incoming = raw.get("general", {}).get("secret")
        if not incoming or not hmac.compare_digest(str(incoming), str(env_secret)):
            log.warning("Webhook rejected: invalid secret")
            raise HTTPException(status_code=401, detail="Unauthorized: invalid webhook secret")


async def _log_invalid_json_body(request: Request) -> None:
    """Log the full request body when JSON parsing fails, with req_id."""
    try:
        body_bytes = await request.body()
        body_text = body_bytes.decode("utf-8", errors="replace")
    except (RuntimeError, UnicodeDecodeError):
        body_text = "<unreadable>"
    req_id = getattr(request.state, "request_id", None)
    log.warning("Invalid JSON body req_id=%s body=%s", req_id, body_text)

def _require_json_content_type(request: Request) -> None:
    """Ensure the request has application/json content type or raise 415."""
    ctype = request.headers.get("content-type", "").lower()
    if "application/json" not in ctype:
        raise HTTPException(
            status_code=415,
            detail="Unsupported Media Type: application/json required",
        )

async def _read_json_body(request: Request) -> dict:
    """Read and parse JSON body; log full body on failure and raise 422."""
    try:
        return await request.json()
    except (ValueError, TypeError, UnicodeDecodeError) as exc:
        await _log_invalid_json_body(request)
        raise HTTPException(status_code=422, detail="Invalid JSON body") from exc

def _validate_schema(raw: dict) -> None:
    """Validate payload against TradingView JSON schema; raise 422 with detail on error."""
    try:
        jsonschema_validate(instance=raw, schema=TRADINGVIEW_SCHEMA)
    except JSONSchemaValidationError as exc:
        raise HTTPException(status_code=422, detail="JSON schema validation error") from exc

def _build_response(
    payload: TradingViewWebhook, *, signal: SignalType, side: Side, symbol: str
) -> dict:
    return {
        "status": "ok",
        "signal": signal.value,
        "side": side.value,
        "symbol": symbol,
        "ticker": payload.general.ticker,
        "action": payload.order.action,
        "contracts": str(payload.order.contracts),
        "price": str(payload.order.price),
        "received_at": datetime.now(timezone.utc).isoformat(),
    }

def _format_telegram_message(
    *,
    payload: TradingViewWebhook,
    symbol: str,
    signal: SignalType,
    side: Side,
    req_id: Optional[str],
) -> str:
    """Format a concise Telegram message for the webhook event.

    Keep locals to a minimum to satisfy lint rules and reduce noise.
    """
    price_text = str(payload.order.price) if payload.order.price is not None else "market"
    lines = [
        "HyperTrade Webhook",
        f"Symbol: {symbol}",
        (
            f"Signal: {signal.value} | Side: {side.value} | Leverage: "
            f"{payload.general.leverage or '-'}"
        ),
        (
            "Order: action="
            f"{payload.order.action} id={payload.order.id} "
            f"contracts={payload.order.contracts} price={price_text}"
        ),
        (
            "Position: "
            f"{payload.market.previous_position}({payload.market.previous_position_size}) -> "
            f"{payload.market.position}({payload.market.position_size})"
        ),
        f"Strategy: {payload.general.strategy or '-'} | Interval: {payload.general.interval}",
        (
            "Times: "
            f"time={payload.general.time.isoformat()} now={payload.general.timenow.isoformat()}"
        ),
    ]
    if payload.order.comment:
        lines.append(f"Comment: {payload.order.comment}")
    if req_id:
        lines.append(f"ReqID: {req_id}")
    return "\n".join(lines)

def require_env(var_name: str) -> str:
    """Raise an exception if env var missing."""
    value = os.getenv(var_name)
    if not value:
        log.info("Missing required environment variable: %s", var_name)
        raise ValueError(var_name + " must be provided")
    return value


# ═══════════════════════════════════════════════════════════════════════════
# History & Analytics Endpoints
# ═══════════════════════════════════════════════════════════════════════════

@history_router.get(
    "/history/orders",
    summary="Get order execution history",
)
async def get_orders_history(
    request: Request,
    limit: int = 100,
    offset: int = 0,
    symbol: Optional[str] = None,
    status: Optional[str] = None,
    side: Optional[str] = None,
) -> dict:
    """Retrieve order execution history from database.

    Args:
        limit: Maximum number of orders to return (default 100, max 1000)
        offset: Number of orders to skip for pagination
        symbol: Filter by trading symbol (e.g., 'ETHUSDT')
        status: Filter by status (PLACED, FAILED, REJECTED)
        side: Filter by side (BUY, SELL)

    Returns:
        Dictionary with orders list and metadata
    """
    db = getattr(request.app.state, "db", None)
    if not db:
        raise HTTPException(status_code=503, detail="Database not available")

    limit = min(max(1, limit), 1000)
    offset = max(0, offset)

    orders = db.get_orders(limit=limit, offset=offset, symbol=symbol, status=status, side=side)
    return {
        "status": "ok",
        "count": len(orders),
        "limit": limit,
        "offset": offset,
        "orders": orders,
    }


@history_router.get(
    "/history/failures",
    summary="Get order failure logs",
)
async def get_failures_history(
    request: Request,
    limit: int = 100,
    offset: int = 0,
    error_type: Optional[str] = None,
) -> dict:
    """Retrieve order failure logs from database.

    Args:
        limit: Maximum number of failures to return (default 100, max 1000)
        offset: Number of failures to skip for pagination
        error_type: Filter by error type

    Returns:
        Dictionary with failures list and metadata
    """
    db = getattr(request.app.state, "db", None)
    if not db:
        raise HTTPException(status_code=503, detail="Database not available")

    limit = min(max(1, limit), 1000)
    offset = max(0, offset)

    failures = db.get_failures(limit=limit, offset=offset, error_type=error_type)
    return {
        "status": "ok",
        "count": len(failures),
        "limit": limit,
        "offset": offset,
        "failures": failures,
    }


@history_router.get(
    "/history/order/{request_id}",
    summary="Get order details by request ID",
)
async def get_order_details(
    request: Request,
    request_id: str,
) -> dict:
    """Get detailed order information and associated failures.

    Args:
        request_id: Unique request identifier

    Returns:
        Dictionary with order details and failure logs
    """
    db = getattr(request.app.state, "db", None)
    if not db:
        raise HTTPException(status_code=503, detail="Database not available")

    order = db.get_order_by_request_id(request_id)
    if not order:
        raise HTTPException(status_code=404, detail=f"Order not found: {request_id}")

    failures = db.get_failures_by_order_id(order["id"]) if order.get("id") else []

    return {
        "status": "ok",
        "order": order,
        "failures": failures,
    }


@history_router.get(
    "/history/stats",
    summary="Get order statistics",
)
async def get_statistics(request: Request) -> dict:
    """Get summary statistics about orders and failures.

    Returns:
        Dictionary with various statistics
    """
    db = getattr(request.app.state, "db", None)
    if not db:
        raise HTTPException(status_code=503, detail="Database not available")

    stats = db.get_statistics()
    return {
        "status": "ok",
        "statistics": stats,
    }
