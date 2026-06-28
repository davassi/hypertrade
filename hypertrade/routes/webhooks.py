"""TradingView webhook endpoint: validate, parse and respond."""

import asyncio
import hashlib
import logging
import hmac
import sqlite3
import time
import json

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from jsonschema import (
    validate as jsonschema_validate,
    ValidationError as JSONSchemaValidationError,
)

from ..config import get_settings
from ..schemas.tradingview_schema import TRADINGVIEW_SCHEMA
from ..schemas.tradingview import TradingViewWebhook
from ..security import require_ip_whitelisted, require_bearer_secret

from ..routes.tradingview_enums import SignalType, PositionType, OrderAction, Side
from ..idempotency import ReserveOutcome
from ..logging import format_log_context

from .hyperliquid_service import (
    HyperliquidService,
    OrderRequest,
    HyperliquidValidationError,
    HyperliquidNetworkError,
    HyperliquidAPIError,
    HyperliquidRejection,
)

router = APIRouter(tags=["webhooks"])
history_router = APIRouter(tags=["history"], dependencies=[Depends(require_bearer_secret)])
log = logging.getLogger("uvicorn.error")

def _derive_cloid(seed: str) -> str:
    """Derive a deterministic Hyperliquid client order id (cloid) from a seed.

    The seed is the request nonce (or, falling back, the request id): it is
    stable across that request's retries AND identical if the very same nonce is
    re-sent later, so the exchange dedupes a duplicate submission by cloid.

    Returns a valid cloid per the SDK's ``Cloid`` contract: ``"0x"`` followed by
    exactly 32 hex chars (16 bytes) — here the first 16 bytes of the SHA-256 of
    the seed.
    """
    return "0x" + hashlib.sha256(seed.encode()).hexdigest()[:32]


async def _place_order_with_retry(client: HyperliquidService, order_request: OrderRequest, max_retries: int = 2) -> dict:
    """Place an order with backoff, querying-before-resubmit to avoid duplicates.

    TD-1: the order submission carries a deterministic cloid (set on
    ``order_request.cloid`` by the caller). On any RETRY, before resubmitting we
    ask the exchange whether the order already landed under that cloid:

      * query raises (network/API) -> re-raise, do NOT resubmit (cannot confirm —
        a duplicate real order is the worse outcome).
      * query returns a found order -> return a result referencing it, do NOT
        resubmit.
      * query returns None (confirmed absent) -> resubmit.

    If ``order_request.cloid`` is unset (defensive — the caller always sets one),
    fall back to the original retry-and-resubmit behavior.

    Args:
        client: HyperliquidService instance
        order_request: Order details (must carry a deterministic cloid)
        max_retries: Maximum number of retry attempts (excluding first attempt)

    Returns:
        Order result from API (or a reference to an already-placed order)

    Raises:
        HyperliquidValidationError: For bad input (no retry)
        HyperliquidAPIError: For persistent API failures (retried)
        HyperliquidNetworkError: For network errors (retried)
    """
    cloid = order_request.cloid
    ctx = format_log_context(
        symbol=order_request.symbol,
        side=getattr(order_request.side, "value", order_request.side),
        cloid=cloid,
        req_id=order_request.req_id,
    )
    for attempt in range(max_retries + 1):
        # Before any RETRY (attempt > 0) carrying a cloid, confirm the order did
        # not already land under that cloid — never resubmit a possibly-live order.
        if attempt > 0 and cloid:
            existing = await asyncio.to_thread(client.find_order_by_cloid, cloid)
            if existing is not None:
                log.warning(
                    "Order already landed under cloid=%s; returning without resubmit",
                    cloid,
                )
                return {
                    "status": "already_placed",
                    "orderId": _extract_oid(existing),
                    "cloid": cloid,
                    "found_order": existing,
                }
            log.info("Exchange confirms cloid=%s absent; resubmitting", cloid)

        try:
            # place_order is synchronous and performs blocking network I/O to
            # Hyperliquid; run it off the event loop so concurrent requests
            # (and the health check) are not stalled while an order is in flight.
            return await asyncio.to_thread(client.place_order, order_request)
        except HyperliquidRejection as e:
            # An exchange REJECTION (insufficient margin / could-not-match / bad price / 4xx) gets
            # exactly ONE fast, fresh-priced retry — a momentary reject can clear on a re-fetched mid —
            # then is surfaced TERMINAL (HyperliquidRejection ⊂ ValidationError → HTTP 400 → the strategy
            # bot pauses/unwinds fast, NEVER the ~1h desk transient-retry). Logged on every attempt.
            if attempt < 1:
                log.warning("Order REJECTED (attempt %d) — retrying once with a fresh price: %s | %s", attempt + 1, str(e), ctx)
                continue
            log.error("Order REJECTED again after one retry — surfacing terminal (no further retry): %s | %s", str(e), ctx)
            raise
        except HyperliquidValidationError as e:
            # Don't retry validation errors - they're permanent
            log.warning("Order validation failed, not retrying: %s | %s | order=%s", str(e), ctx, order_request)
            raise
        except (HyperliquidNetworkError, HyperliquidAPIError) as e:
            if attempt < max_retries:
                wait_time = 2 ** attempt  # Exponential backoff: 1s, 2s, 4s...
                log.warning(
                    "Order placement attempt %d/%d failed, retrying in %ds: %s | %s",
                    attempt + 1, max_retries + 1, wait_time, str(e), ctx
                )
                await asyncio.sleep(wait_time)
            else:
                log.error("Order placement failed after %d attempts: %s | %s", max_retries + 1, str(e), ctx)
                raise


def _extract_oid(found_order: dict) -> Optional[int]:
    """Best-effort extraction of the numeric order id from an orderStatus payload.

    Hyperliquid nests it as ``order.order.oid``; tolerate missing keys so logging
    a found order never raises.
    """
    try:
        return found_order["order"]["order"]["oid"]
    except (KeyError, TypeError):
        return None

@router.post(
    "/webhook",
    dependencies=[Depends(require_ip_whitelisted(None))],
    summary="TradingView → Hyperliquid",
)
async def hypertrade_webhook(
    request: Request,
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

    idempotency = getattr(request.app.state, "idempotency", None)
    nonce = payload.general.nonce
    if idempotency is not None and not nonce:
        raise HTTPException(status_code=400, detail="general.nonce is required")

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
    
    # Builder-deployed (HIP-3) dex coins are dex-qualified ("xyz:KR200") and the
    # dex prefix is case-sensitive on Hyperliquid — uppercasing corrupts it to
    # "XYZ:KR200". Normalize case only for plain (Binance-style) bases; pass
    # dex-qualified coins through verbatim.
    raw_base = payload.currency.base
    symbol = raw_base if ":" in raw_base else raw_base.upper()
    # contracts/price are already validated as Decimal by the Pydantic model;
    # keep them as Decimal so exchange-bound sizing/pricing stays exact (a float
    # round-trip corrupts precision, e.g. Decimal(0.1_float) != Decimal("0.1")).
    contracts = payload.order.contracts
    price = payload.order.price

    # Fifth: processing logic here (TODO: would be good to enqueue the job).
    nominal_quantity = contracts * price
    
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

    # Determine reduce_only flag based on signal type
    # REDUCE signals should only reduce existing positions, not open new ones
    reduce_only = signal in {SignalType.REDUCE_LONG, SignalType.REDUCE_SHORT}

    # Deterministic cloid (TD-1): stable across this request's retries and across a
    # re-send of the SAME nonce, so the exchange dedupes a duplicate submission and
    # the retry loop can query-before-resubmit. Seed from nonce (preferred) or the
    # request id (always present) so a cloid is always available.
    req_id = getattr(request.state, "request_id", None)
    cloid_seed = nonce or req_id
    cloid = _derive_cloid(cloid_seed) if cloid_seed else None

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
        cloid=cloid,
        req_id=req_id,
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

    # Dry-run / demo: the full pipeline above is exercised, but nothing leaves
    # the process — no exchange call, no DB write, no idempotency.
    # Keep any new external/side-effecting call BELOW this branch — above it runs in dry-run too.
    if settings.dry_run:
        log.info(
            "DRY-RUN: order NOT placed | %s %s qty=%s price=%s lev=%sx reduce_only=%s",
            symbol,
            side.value,
            order_request.qty,
            order_request.price,
            order_request.leverage or 1,
            order_request.reduce_only,
        )
        return _build_dry_run_response(
            payload, signal=signal, side=side, symbol=symbol, order_request=order_request
        )

    client = HyperliquidService(
        base_url=settings.api_url,
        master_addr=settings.master_addr,
        api_wallet_priv=settings.api_wallet_priv.get_secret_value(),
        subaccount_addr=vault_address,
    )

    # ===================================================================
    # EXECUTION: Place the order with retry logic.
    # ===================================================================

    req_id = getattr(request.state, "request_id", None)
    db = getattr(request.app.state, "db", None)

    if idempotency is not None:
        try:
            reservation = idempotency.reserve(
                nonce, req_id, get_settings().idempotency_inflight_timeout
            )
        except sqlite3.Error as exc:
            log.warning("Idempotency store unavailable during reserve: %s", exc)
            raise HTTPException(
                status_code=503, detail="Idempotency store unavailable"
            ) from exc
        if reservation.outcome is ReserveOutcome.DUPLICATE_COMPLETED:
            return JSONResponse({**(reservation.result or {}), "status": "duplicate"})
        if reservation.outcome is ReserveOutcome.IN_FLIGHT:
            raise HTTPException(status_code=409, detail="Duplicate request in flight")

    placed_ok = False
    try:
        log.info("Attempting to place order on Hyperliquid: symbol=%s side=%s", symbol, side.value)
        result = await _place_order_with_retry(client, order_request, max_retries=2)
        placed_ok = True
    except HyperliquidValidationError as e:
        log.warning(
            "Order validation error: %s | %s", e,
            format_log_context(req_id=req_id, cloid=cloid, symbol=symbol, side=side.value),
        )
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
        log.error(
            "Network error placing order (after retries): %s | %s", e,
            format_log_context(req_id=req_id, cloid=cloid, symbol=symbol, side=side.value),
        )
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
        log.error(
            "API error placing order (after retries): %s | %s", e,
            format_log_context(req_id=req_id, cloid=cloid, symbol=symbol, side=side.value),
        )
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
    finally:
        if idempotency is not None and not placed_ok:
            try:
                idempotency.release(nonce)
            except sqlite3.Error as exc:
                # Best-effort: a release failure must not mask the original placement exception.
                log.warning("Idempotency store unavailable during release (nonce=%s): %s", nonce, exc)

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

    if idempotency is not None:
        # complete() persists the plain dict body for replay; _build_response must return a dict (not a Response object).
        # Best-effort: a failed complete leaves the nonce in_progress to be reclaimed after the in-flight timeout.
        try:
            idempotency.complete(nonce, response)
        except sqlite3.Error as exc:
            log.warning("Idempotency store unavailable during complete (nonce=%s): %s", nonce, exc)

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

def _build_dry_run_response(
    payload: TradingViewWebhook,
    *,
    signal: SignalType,
    side: Side,
    symbol: str,
    order_request: OrderRequest,
) -> dict:
    """Mirror of `_build_response` for dry-run: echo the order that *would* be sent."""
    return {
        "status": "dry_run",
        "signal": signal.value,
        "side": side.value,
        "symbol": symbol,
        "ticker": payload.general.ticker,
        "action": payload.order.action,
        "contracts": str(order_request.qty),
        "price": str(order_request.price),
        "leverage": order_request.leverage,
        "reduce_only": order_request.reduce_only,
        "subaccount": order_request.subaccount,
        "received_at": datetime.now(timezone.utc).isoformat(),
    }

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
