import logging
from fastapi import FastAPI
from pydantic import ValidationError

from .config import get_settings
from .logging import setup_logging, log_startup_banner, log_endpoints
from .middleware.logging import LoggingMiddleware
from .middleware.content_limit import ContentLengthLimitMiddleware
from .routes.health import router as health_router
from .routes.webhooks import router as webhooks_router
from .exception_handlers import register_exception_handlers
from starlette.middleware.trustedhost import TrustedHostMiddleware

log = logging.getLogger("uvicorn.error")

# Change the port number and start the daemon with: 
# 
# $ uvicorn hypertrade.daemon:app --host 0.0.0.0 --port 9414
#
def create_daemon() -> FastAPI:
    
    # Configure baseline logging early (INFO until settings are loaded)
    settings = get_settings()
    setup_logging(settings.log_level)

    app = FastAPI(title="Hypertrade Daemon", version="1.0.0")
    
    # Load and attach settings at startup; raise a clear error if env is missing
    try:  
        app.state.settings = settings
    except ValidationError as e:
        msg = (
            "Missing required environment variables: "
            + ", ".join("HYPERTRADE_MASTER_ADDR","HYPERTRADE_API_WALLET_PRIV", "HYPERTRADE_SUBACCOUNT_ADDR")
            + ". Export them in your shell or set them in .env."
        )
        raise RuntimeError(msg) from e

    # Finalize logging with configured level and add middleware
    
    app.add_middleware(LoggingMiddleware)
    app.add_middleware(ContentLengthLimitMiddleware, max_bytes=settings.max_payload_bytes)
    if settings.enable_trusted_hosts and settings.trusted_hosts:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.trusted_hosts)
    register_exception_handlers(app)
    log.info(
        "App started env=%s whitelist_enabled=%s log_level=%s",
        settings.environment,
        settings.ip_whitelist_enabled,
        settings.log_level,
    )
    log.info("Loaded %d TV webhook IPs", len(settings.tv_webhook_ips or []))
    
    # Showing our startup banner
    log_startup_banner(
        host=None,
        port=None,
        whitelist_enabled=settings.ip_whitelist_enabled,
        whitelist_ips=settings.tv_webhook_ips,
        trust_xff=settings.trust_forwarded_for,
    )

    # Setting the Routers up
    app.include_router(health_router)
    app.include_router(webhooks_router)

    # Log endpoints after routes are registered
    log_endpoints(app)
    
    return app

# Expose ASGI app for uvicorn
app = create_daemon()
