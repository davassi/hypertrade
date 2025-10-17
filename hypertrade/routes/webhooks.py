import logging
from datetime import datetime, timezone

import hmac
from fastapi import APIRouter, Depends, Body, HTTPException, Request
from jsonschema import validate as jsonschema_validate, ValidationError as JSONSchemaValidationError

from ..schemas.tradingview_schema import TRADINGVIEW_SCHEMA
from ..schemas.tradingview import TradingViewWebhook
from ..security import require_ip_whitelisted

router = APIRouter(tags=["webhooks"])

log = logging.getLogger("uvicorn.error")

@router.post("/webhook", dependencies=[Depends(require_ip_whitelisted(None))], summary="TradingView webhook")
async def tradingview_webhook(request: Request, raw: dict = Body(...)) -> dict:
    
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
    
    # Fourth: processing logic here (would be good to enqueue the job)
    
    # Finally: build a response
    response = {
        "status": "ok",
        "ticker": payload.general.ticker,
        "exchange": payload.general.exchange,
        "action": payload.order.action,
        "contracts": str(payload.order.contracts),
        "price": str(payload.order.price),
        "received_at": datetime.now(timezone.utc).isoformat(),
    }
    
    # Optional: shoot the response on telegram 
    # TODO
    
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
