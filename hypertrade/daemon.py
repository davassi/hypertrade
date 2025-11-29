"""ASGI app factory and configuration for the Hypertrade daemon."""

import logging
import multiprocessing
import os
import signal
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import ValidationError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import get_settings
from .logging import log_startup_banner, log_endpoints, configure_logging
from .middleware.logging import LoggingMiddleware
from .middleware.content_limit import ContentLengthLimitMiddleware
from .middleware.rate_limit import RateLimitMiddleware
from .routes.health import router as health_router
from .routes.webhooks import router as webhooks_router, history_router
from .routes.admin import router as admin_router
from .notify import send_telegram_message
from .exception_handlers import register_exception_handlers
from .database import OrderDatabase

log = logging.getLogger("uvicorn.error")

def _please_die_gracefully() -> None:
    """Log a clear error message and exit if required secrets are missing."""

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
        "    • HYPERTRADE_SUBACCOUNT_ADDR  → your sub-account address (optional)\n"
        "\n\n"

        "The daemon will start automatically once these are set.\n"
    )

    log.info(banner)
    log.critical("Hypertrade startup aborted: missing required secrets")
    _stop_parent_supervisor()
    sys.exit(1)


def _stop_parent_supervisor() -> None:
    """Signal uvicorn's parent process (if any) so reload/workers also exit."""

    parent = multiprocessing.parent_process()
    if not parent:
        return

    ppid = parent.pid
    if not ppid or ppid == os.getpid():
        return

    try:
        os.kill(ppid, signal.SIGTERM)
        log.warning("Signaled parent process pid=%s to exit", ppid)
    except OSError as exc:
        # Parent already dead or signal not permitted; ignore and rely on sys.exit.
        log.debug("Unable to signal parent process %s to exit: %s", ppid, exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage app startup and shutdown events."""
    
    # Startup: Log that the daemon is ready
    settings = get_settings()
    
    log.info("Hypertrade Daemon is ready to accept requests on: '%s'",
        settings.subaccount_addr or "MASTER ACCOUNT")

    if not settings.subaccount_addr:
        log.warning(
            "Trading on MASTER account (no sub-account set)! This is NOT recommended for safety."
        )
    else:
        log.info("Trading restricted to subaccount: %s", settings.subaccount_addr)

    yield

    # Shutdown: Log that the daemon is shutting down
    log.info("Hypertrade Daemon is shutting down.")


def create_daemon() -> FastAPI:
    """Create and configure the FastAPI app."""

    # Create app first so we can attach settings or fail cleanly
    app = FastAPI(title="Hypertrade Daemon", version="1.0.0", lifespan=lifespan)

    # Load settings and configure logging; provide clear error if env missing
    try:
        settings = get_settings()
    except ValidationError:
        _please_die_gracefully()

    # Configure logging based on settings
    configure_logging(settings.log_level)

    app.state.settings = settings

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

        app.state.telegram_notify = _telegram_notify
        log.info("Telegram notifications enabled")
    else:
        app.state.telegram_notify = None

    # Initialize database if enabled
    if getattr(settings, "db_enabled", True):
        try:
            db = OrderDatabase(settings.db_path)
            app.state.db = db
            log.info("Order database initialized at: %s", settings.db_path)
        except Exception as e:
            log.error("Failed to initialize database: %s", e)
            raise
    else:
        app.state.db = None
        log.info("Database persistence disabled")

    # Finalize logging with configured level and add middleware
    app.add_middleware(LoggingMiddleware)
    app.add_middleware(
        ContentLengthLimitMiddleware, max_bytes=settings.max_payload_bytes
    )
    if settings.rate_limit_enabled:
        whitelist = settings.tv_webhook_ips if settings.ip_whitelist_enabled else []
        app.add_middleware(
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
        app.add_middleware(
            TrustedHostMiddleware, allowed_hosts=settings.trusted_hosts
        )
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
        host=settings.listen_host,
        port=settings.listen_port,
        whitelist_enabled=settings.ip_whitelist_enabled,
        whitelist_ips=settings.tv_webhook_ips,
        trust_xff=settings.trust_forwarded_for,
    )

    # Setting the Routers up
    app.include_router(health_router)
    app.include_router(webhooks_router)
    app.include_router(history_router)
    app.include_router(admin_router)

    # Log endpoints after routes are registered
    log_endpoints(app)

    return app


# Expose ASGI app for uvicorn
app = create_daemon()
