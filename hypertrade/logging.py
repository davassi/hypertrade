"""Logging utilities: configure Uvicorn-compatible logs and startup banner."""

import logging as pylog
from typing import Iterable, Optional

try:  # FastAPI might not be installed in lint-only environments
    from fastapi.routing import APIRoute
except ImportError:  # pragma: no cover
    APIRoute = None  # type: ignore[assignment]

from .version import __version__

# Use uvicorn.error logger (guaranteed to exist + colored in dev)
log = pylog.getLogger("uvicorn.error")


def format_log_context(**fields: object) -> str:
    """Format diagnostic fields as a space-separated ``key=value`` string.

    Used to build a uniform correlation/context suffix for failure log lines so
    the prefix is defined once (no copy-paste across call sites). Skips fields
    whose value is ``None``; never raises.
    """
    return " ".join(f"{key}={value}" for key, value in fields.items() if value is not None)


def configure_logging(log_level: str = "INFO") -> None:
    """Configure Python logging to use the specified level."""
    level = log_level.upper()
    valid_levels = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}

    if level not in valid_levels:
        level = "INFO"

    # Configure uvicorn loggers
    pylog.getLogger("uvicorn").setLevel(level)
    pylog.getLogger("uvicorn.access").setLevel(level)
    pylog.getLogger("uvicorn.error").setLevel(level)

    # Configure app logger
    pylog.getLogger("hypertrade").setLevel(level)

# pylint: disable=too-many-arguments
def log_startup_banner(
    *,
    host: Optional[str] = None,
    port: Optional[int] = None,
    whitelist_enabled: bool = False,
    whitelist_ips: Iterable[str] = (),
    trust_xff: bool = True,
    version: str = "1.0.0",  # pass __version__ or from importlib.metadata
) -> None:
    """Log the startup banner with configuration details."""
    url = f"http://{host}:{port}" if port else f"http://{host}:<port from HYPERTRADE_LISTEN_PORT>"
    ip_count = len(set(str(ip) for ip in whitelist_ips if ip))

    # Hypertrade ASCII art (compact + readable)
    art = [
        "██╗  ██╗██╗   ██╗██████╗ ███████╗██████╗ ████████╗██████╗  █████╗ ██████╗ ███████╗\n",
        "██║  ██║╚██╗ ██╔╝██╔══██╗██╔══██╗██╔══██╗╚══██╔══╝██╔══██╗██╔══██╗██╔══██╗██╔════╝\n",
        "███████║ ╚████╔╝ ██████╔╝█████╔╝ ██████╔╝   ██║   ██████╔╝███████║██║  ██║█████╗  \n",
        "██╔══██║  ╚██╔╝  ██╔═══╝ ██╔══╝  ██╔══██╗   ██║   ██╔══██╗██╔══██║██║  ██║██╔══╝  \n",
        "██║  ██║   ██║   ██║     ███████╗██║  ██║   ██║   ██║  ██║██║  ██║██████╔╝███████╗\n",
        "╚═╝  ╚═╝   ╚═╝   ╚═╝     ╚══════╝╚═╝  ╚═╝   ╚═╝   ╚═╝  ╚═╝╚═╝  ╚═╝╚═════╝ ╚══════╝\n",
    ]

    ruler = "═" * 88

    banner = f"""
{ruler}
{"".join(art)}
{ruler}
        Hypertrade Webhook Daemon v{version}
        
        Listening → {url}
        IP Whitelist → {'ENABLED' if whitelist_enabled else 'disabled'} ({ip_count} IP{'s' if ip_count != 1 else ''})
        Trust X-Forwarded-For → {'YES' if trust_xff else 'NO'}
        
        Ready for TradingView webhooks!
{ruler}
    """

    for line in banner.strip().splitlines():
        log.info(line)


def log_endpoints(app) -> None:
    """Log all registered APIRoute endpoints."""
    if APIRoute is None:
        log.info("FastAPI not available; skipping endpoint log.")
        return

    lines = []
    for route in getattr(app, "routes", []):
        if isinstance(route, APIRoute):
            methods = sorted(
                m for m in (route.methods or []) if m not in {"HEAD", "OPTIONS"}
            )
            method_str = ",".join(methods) or "-"
            lines.append((route.path, method_str, route.name))
    
    lines.sort(key=lambda x: (x[0], x[1]))
    header = f"Available endpoints ({len(lines)}):"
    
    log.info("%s", header)
    for path, methods, name in lines:
        log.info("  %-7s %-40s (%s)", methods, path, name)
