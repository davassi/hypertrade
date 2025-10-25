import logging
import hmac

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Body, HTTPException, Request
from fastapi import BackgroundTasks
from fastapi.responses import JSONResponse
from jsonschema import validate as jsonschema_validate, ValidationError as JSONSchemaValidationError

from ..schemas.tradingview_schema import TRADINGVIEW_SCHEMA
from ..schemas.tradingview import TradingViewWebhook
from ..security import require_ip_whitelisted
from ..notify import send_telegram_message

from ..routes.tradingview_enums import SignalType, PositionType, OrderAction, Side

router = APIRouter(tags=["webhooks"])

log = logging.getLogger("uvicorn.error")

@router.post("/webhook", dependencies=[Depends(require_ip_whitelisted(None))], summary="TradingView webhook")
async def tradingview_webhook(request: Request, background_tasks: BackgroundTasks, raw: dict = Body(...)) -> dict:
    
    # Let's start with our checks.
    
    # First: Require JSON content type
    ctype = request.headers.get("content-type", "").lower()
    if "application/json" not in ctype:
        raise HTTPException(status_code=415, detail="Unsupported Media Type: application/json required")
    
    # Second: JSON Schema validation on raw payload
    try:
        jsonschema_validate(instance=raw, schema=TRADINGVIEW_SCHEMA)
    except JSONSchemaValidationError as e:
        path = ".".join([str(p) for p in e.path])
        detail = f"JSON schema validation error at '{path or '$'}': {e.message}"
        raise HTTPException(status_code=422, detail=detail)

    # Third (Optional but recommended) secret enforcement: if env secret is set, 
    # then the payload requires to carry a matching general.secret
    secret_enforcement(request, raw)

    # Pydantic parsing for strong typing and coercion
    payload = TradingViewWebhook.model_validate(raw)
    
    # Now log a summary of the webhook payload
    log.info("Received TradingView webhook")
    log.info(
        "\x1b[31mTradingView webhook: [%s %s %s] -> ACTION %s@%s contracts=%s ['%s'] alert='%s'\x1b[0m",
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
    
    # Fourth: processing logic here (TOOD: would be good to enqueue the job)
    signal = parse_signal(payload)
    log.info("Parsed signal: %s", signal.value)
    symbol = payload.general.ticker.upper()
    log.info("TradingView ticker mapped to symbol: %s", symbol)
    
    try:
        contracts = float(payload.order.contracts)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid 'contracts' value")
    
    price = float(payload.order.price) if payload.order.price else None

    side = signal_to_side(signal)

    if side is None or signal == SignalType.NO_ACTION:
        log.info("No actionable signal. signal=%s payload_id=%s", signal.value, payload.order.id)
        return JSONResponse({"status": "no_action", "signal": signal.value, "order_id": payload.order.id})

    # Execute plugging into Hyperliquid SDK 
    #try:
        # result = client.place_order(symbol=symbol, side=side, qty=contracts, price=price, subaccount=subaccount)
    #except Exception as e:
    #    log.exception("Order placement failed")
    #    raise HTTPException(status_code=502, detail=f"Order placement failed: {e}")

    
    # Finally: build a response
    response = {
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
    
    # Optional: shoot the response to Telegram if configured (pre-bound at startup)
    notifier = getattr(request.app.state, "telegram_notify", None)
    if notifier:
        text = (
            f"[{symbol}] {signal.value} {side.value} price={payload.order.price}\n"
        )
        background_tasks.add_task(notifier, text)
    
    return response

# Check the webhook secret if configured in environment
def secret_enforcement(request, raw):
    settings = request.app.state.settings
    env_secret = None
    
    if getattr(settings, "webhook_secret", None):
        env_secret = settings.webhook_secret.get_secret_value()
    
    if env_secret:
        incoming = None
        try:
            incoming = raw.get("general", {}).get("secret")
        except Exception:
            incoming = None
    
        if not incoming or not hmac.compare_digest(str(incoming), str(env_secret)):
            log.warning("Webhook rejected: invalid secret")
            raise HTTPException(status_code=401, detail="Unauthorized: invalid webhook secret")

# Enums and parsing logic 
def signal_to_side(signal: SignalType) -> Optional[Side]:
    if signal in (SignalType.OPEN_LONG, SignalType.CLOSE_SHORT, SignalType.ADD_LONG, SignalType.REVERSE_TO_LONG, SignalType.REDUCE_SHORT):
        return Side.BUY
    if signal in (SignalType.OPEN_SHORT, SignalType.CLOSE_LONG, SignalType.ADD_SHORT, SignalType.REVERSE_TO_SHORT, SignalType.REDUCE_LONG):
        return Side.SELL
    return None

def parse_signal(payload: TradingViewWebhook) -> SignalType:
    """Return a normalized SignalType based on payload contents using Enums."""
    # Coerce to enums safely
    try:
        current = PositionType(payload.market.position.lower())
    except Exception:
        current = PositionType.FLAT
    try:
        previous = PositionType(payload.market.previous_position.lower()) if payload.market.previous_position else PositionType.FLAT
    except Exception:
        previous = PositionType.FLAT
    try:
        action = OrderAction(payload.order.action.lower())
    except Exception:
        return SignalType.NO_ACTION

    # Open/Close
    if previous == PositionType.FLAT and current == PositionType.LONG and action == OrderAction.BUY:
        return SignalType.OPEN_LONG
    if previous == PositionType.LONG and current == PositionType.FLAT and action == OrderAction.SELL:
        return SignalType.CLOSE_LONG
    if previous == PositionType.FLAT and current == PositionType.SHORT and action == OrderAction.SELL:
        return SignalType.OPEN_SHORT
    if previous == PositionType.SHORT and current == PositionType.FLAT and action == OrderAction.BUY:
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
