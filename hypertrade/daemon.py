"""ASGI app factory and configuration for the Hypertrade daemon."""

import logging
from fastapi import FastAPI
from pydantic import ValidationError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import get_settings
from .logging import setup_logging, log_startup_banner, log_endpoints
from .middleware.logging import LoggingMiddleware
from .middleware.content_limit import ContentLengthLimitMiddleware
from .middleware.rate_limit import RateLimitMiddleware
from .routes.health import router as health_router
from .routes.webhooks import router as webhooks_router
from .notify import send_telegram_message
from .exception_handlers import register_exception_handlers

log = logging.getLogger("uvicorn.error")

import sys

def die_gracefully() -> None:
    """
    Just print a gorgeous, helpful error and exit immediately.
    """
    banner = (
        "\n"
        "╔" + "═" * 72 + "╗\n"
        "║  ⚠️   HYPERTRADE DAEMON CANNOT START – MISSING SECRETS   ⚠️              ║\n"
        "╚" + "═" * 72 + "╝\n"
        "\n"
        "Required environment variables are not set:\n"
        "\n"
        "    • HYPERTRADE_MASTER_ADDR      → your Hyperliquid master address\n"
        "    • HYPERTRADE_API_WALLET_PRIV  → 64-char hex private key (with or without 0x)\n"
        "    • HYPERTRADE_SUBACCOUNT_ADDR  → your sub-account address\n"
        "\n"
        "Fix it by one of these methods:\n"
        "\n"
        "1. Create a .env file in project root:\n"
        "       HYPERTRADE_MASTER_ADDR=addr1q...\n"
        "       HYPERTRADE_API_WALLET_PRIV=0123456789abcdef...\n"
        "       HYPERTRADE_SUBACCOUNT_ADDR=addr1q...\n"
        "\n"
        "2. Export in your shell:\n"
        "       export HYPERTRADE_MASTER_ADDR=addr1q...\n"
        "       export HYPERTRADE_API_WALLET_PRIV=...\n"
        "       export HYPERTRADE_SUBACCOUNT_ADDR=addr1q...\n"
        "\n"
        "The daemon will start automatically once these are set.\n"
    )

    print(banner, file=sys.stderr, flush=True)
    log.critical("Hypertrade startup aborted: missing required secrets")
    sys.exit(1)


def create_daemon() -> FastAPI:
    """Create and configure the FastAPI app."""

    # Create app first so we can attach settings or fail cleanly
    app_ = FastAPI(title="Hypertrade Daemon", version="1.0.0")

    # Load settings and configure logging; provide clear error if env missing
    try:
        settings = get_settings()
    except ValidationError:
        die_gracefully()
    
    setup_logging(
        settings.log_level,
        suppress_access=settings.suppress_access_logs,
        suppress_invalid_http_warnings=settings.suppress_invalid_http_warnings,
    )
    app_.state.settings = settings

    # Pre-bind optional Telegram notifier to avoid per-request env access
    if (
        getattr(settings, "telegram_enabled", True)
        and getattr(settings, "telegram_bot_token", None)
        and getattr(settings, "telegram_chat_id", None)
    ):
        token = settings.telegram_bot_token
        chat_id = settings.telegram_chat_id

        def _telegram_notify(text: str, _token=token, _chat_id=chat_id):
            return send_telegram_message(_token, _chat_id, text)

        app_.state.telegram_notify = _telegram_notify
        log.info("Telegram notifications enabled")
    else:
        app_.state.telegram_notify = None

    # Finalize logging with configured level and add middleware
    app_.add_middleware(LoggingMiddleware)
    app_.add_middleware(
        ContentLengthLimitMiddleware, max_bytes=settings.max_payload_bytes
    )
    if settings.rate_limit_enabled:
        whitelist = settings.tv_webhook_ips if settings.ip_whitelist_enabled else []
        app_.add_middleware(
            RateLimitMiddleware,
            max_requests=settings.rate_limit_max_requests,
            window_seconds=settings.rate_limit_window_seconds,
            burst=settings.rate_limit_burst,
            trust_forwarded_for=settings.trust_forwarded_for,
            only_paths=settings.rate_limit_only_paths,
            exclude_paths=settings.rate_limit_exclude_paths,
            whitelist_ips=whitelist,
        )
    if settings.enable_trusted_hosts and settings.trusted_hosts:
        app_.add_middleware(
            TrustedHostMiddleware, allowed_hosts=settings.trusted_hosts
        )
    register_exception_handlers(app_)
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
    app_.include_router(health_router)
    app_.include_router(webhooks_router)

    # Log endpoints after routes are registered
    log_endpoints(app_)

    return app_


# Expose ASGI app for uvicorn
app = create_daemon()
