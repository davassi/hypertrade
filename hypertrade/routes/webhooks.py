"""TradingView webhook endpoint: validate, parse and respond."""

import logging
import hmac

from datetime import datetime, timezone
from typing import Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Request, BackgroundTasks
from fastapi.responses import JSONResponse
from jsonschema import (
    validate as jsonschema_validate,
    ValidationError as JSONSchemaValidationError,
)

from ..schemas.tradingview_schema import TRADINGVIEW_SCHEMA
from ..schemas.tradingview import TradingViewWebhook
from ..security import require_ip_whitelisted

from ..routes.tradingview_enums import SignalType, PositionType, OrderAction, Side

router = APIRouter(tags=["webhooks"])

log = logging.getLogger("uvicorn.error")

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
    except JSONSchemaValidationError as e:
        path = ".".join([str(p) for p in e.path])
        detail = f"JSON schema validation error at '{path or '$'}': {e.message}"
        raise HTTPException(status_code=422, detail=detail)

def _build_response(
    payload: TradingViewWebhook, *, signal: SignalType, side: Side, symbol: str
) -> dict:
    return {
        "status": "ok",
        "signal": signal.value,
        "side": side.value,
        "symbol": symbol,
        "ticker": payload.general.ticker,
        "exchange": payload.general.exchange,
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
    # Prefer original precision from payload where possible
    contracts_text = str(payload.order.contracts)
    price_text = (
        str(payload.order.price) if payload.order.price is not None else "market"
    )
    leverage = payload.general.leverage or "-"
    strategy = payload.general.strategy or "-"
    exchange = payload.general.exchange
    interval = payload.general.interval
    prev_pos = payload.market.previous_position
    prev_sz = str(payload.market.previous_position_size)
    cur_pos = payload.market.position
    cur_sz = str(payload.market.position_size)
    order_id = payload.order.id
    comment = payload.order.comment or "-"
    t_time = payload.general.time.isoformat()
    t_now = payload.general.timenow.isoformat()

    lines = [
        "HyperTrade Webhook",
        f"Symbol: {symbol} @ {exchange}",
        f"Signal: {signal.value} | Side: {side.value} | Leverage: {leverage}",
        (
            "Order: action="
            f"{payload.order.action} id={order_id} "
            f"contracts={contracts_text} price={price_text}"
        ),
        f"Position: {prev_pos}({prev_sz}) -> {cur_pos}({cur_sz})",
        f"Strategy: {strategy} | Interval: {interval}",
        f"Times: time={t_time} now={t_now}",
    ]
    if comment and comment != "-":
        lines.append(f"Comment: {comment}")
    if req_id:
        lines.append(f"ReqID: {req_id}")
    return "\n".join(lines)

def _parse_contracts_and_price(
    payload: TradingViewWebhook,
) -> Tuple[float, Optional[float]]:
    """Parse numeric fields from payload with proper error handling."""
    try:
        contracts = float(payload.order.contracts)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Invalid 'contracts' value") from exc
    price = float(payload.order.price) if payload.order.price else None
    return contracts, price

@router.post(
    "/webhook",
    dependencies=[Depends(require_ip_whitelisted(None))],
    summary="TradingView webhook",
)
async def tradingview_webhook(
    request: Request, background_tasks: BackgroundTasks
) -> dict:
    """Main webhook endpoint: validates, parses, logs, and returns a summary."""
    # Let's start with our checks.

    # First: Require JSON content type
    _require_json_content_type(request)

    # Second: Parse JSON body ourselves to avoid pre-validation errors on non-JSON content
    raw = await _read_json_body(request)

    # Third: JSON Schema validation on raw payload
    _validate_schema(raw)

    # Fourth (Optional but recommended) secret enforcement: if env secret is set,
    # then the payload requires to carry a matching general.secret
    secret_enforcement(request, raw)

    # Pydantic parsing for strong typing and coercion
    payload = TradingViewWebhook.model_validate(raw)

    # Now log a summary of the webhook payload
    log.info("Received TradingView webhook")
    log.info(
        (
            "\x1b[31mTradingView webhook: [%s %s %s] -> ACTION %s@%s "
            "contracts=%s ['%s'] alert='%s'\x1b[0m"
        ),
        payload.general.exchange,
        payload.general.ticker,
        payload.general.interval,
        payload.order.action,
        payload.order.price,
        payload.order.contracts,
        payload.general.time,
        payload.order.alert_message if payload.order.alert_message else "None",
    )
    log.debug("Full webhook payload: %s", raw)

    # Fifth: processing logic here (TODO: would be good to enqueue the job)
    signal = parse_signal(payload)
    log.info("Parsed signal: %s", signal.value)
    symbol = payload.general.ticker.upper()
    log.info("TradingView ticker mapped to symbol: %s", symbol)

    contracts, price = _parse_contracts_and_price(payload)

    side = signal_to_side(signal)

    if side is None or signal == SignalType.NO_ACTION:
        log.info(
            "No actionable signal. signal=%s payload_id=%s",
            signal.value,
            payload.order.id,
        )
        return JSONResponse(
            {"status": "no_action", "signal": signal.value, "order_id": payload.order.id}
        )

    # Execute plugging into Hyperliquid SDK
    # try:
    #     result = client.place_order(
    #         symbol=symbol,
    #         side=side,
    #         qty=contracts,
    #         price=price,
    #         subaccount=subaccount,
    #     )
    #except Exception as e:
    #    log.exception("Order placement failed")
    #    raise HTTPException(status_code=502, detail=f"Order placement failed: {e}")

    # Finally: build a response
    response = _build_response(payload, signal=signal, side=side, symbol=symbol)

    # Optional: shoot the response to Telegram if configured
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

    return response

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

# Enums and parsing logic
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
