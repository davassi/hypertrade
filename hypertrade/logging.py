"""Logging utilities: configure Uvicorn-compatible logs and startup banner."""

import logging as pylog
import logging.config as logging_config
from typing import Iterable, Optional, List
from fastapi.routing import APIRoute
from uvicorn.config import LOGGING_CONFIG

from .version import __version__

import logging
from typing import Iterable, Optional

# Use uvicorn.error logger (guaranteed to exist + colored in dev)
logger = logging.getLogger("uvicorn.error")

# pylint: disable=too-few-public-methods
class _MessageFilter(pylog.Filter):
    def __init__(self, *, deny_contains: Optional[List[str]] = None):
        super().__init__()
        self.deny_contains = deny_contains or []

    def filter(self, record: pylog.LogRecord) -> bool:  # True -> keep
        msg = record.getMessage()
        for frag in self.deny_contains:
            if frag in msg:
                return False
        return True


def setup_logging(
    level: str = "INFO",
    *,
    suppress_access: bool = False,
    suppress_invalid_http_warnings: bool = True,
) -> None:
    """Configure logging to use Uvicorn's format when possible.

    - If Uvicorn hasn't configured logging (e.g., running `python -m app`), apply
      Uvicorn's default LOGGING_CONFIG so our logs match its format.
    - Otherwise, only adjust levels so our loggers integrate with Uvicorn's handlers.
    """
    numeric = getattr(pylog, level, pylog.INFO)
    root = pylog.getLogger()
    if not root.handlers:
        cfg = LOGGING_CONFIG.copy()
        loggers_cfg = cfg.get("loggers", {})
        if "uvicorn.error" in loggers_cfg:
            loggers_cfg["uvicorn.error"]["level"] = level
        if "uvicorn.access" in loggers_cfg:
            loggers_cfg["uvicorn.access"]["level"] = (
                "WARNING" if suppress_access else level
            )
        logging_config.dictConfig(cfg)

    pylog.getLogger("uvicorn").setLevel(numeric)
    err_logger = pylog.getLogger("uvicorn.error")
    err_logger.setLevel(numeric)
    if suppress_invalid_http_warnings:
        err_logger.addFilter(_MessageFilter(deny_contains=["Invalid HTTP request received."]))
    pylog.getLogger("uvicorn.access").setLevel(pylog.WARNING if suppress_access else numeric)


def log_startup_banner(
    *,
    host: Optional[str] = None,
    port: Optional[int] = None,
    whitelist_enabled: bool = False,
    whitelist_ips: Iterable[str] = (),
    trust_xff: bool = True,
    version: str = "1.0.0",  # pass __version__ or from importlib.metadata
) -> None:
    """
    Log a gorgeous, colorful startup banner with Hypertrade ASCII art.
    Uses uvicorn.error logger → automatically colored in dev, clean in prod.
    """
    # Resolve listening URL
    if host and port:
        url = f"http://{host}:{port}"
    elif host:
        url = f"http://{host}"
    else:
        url = "uvicorn default"

    # Count unique IPs safely
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
        logger.info(line)


def log_endpoints(app) -> None:
    """Log all registered APIRoute endpoints."""
    logger = pylog.getLogger("uvicorn.error")
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
    logger.info("%s", header)
    for path, methods, name in lines:
        logger.info("  %-7s %-40s (%s)", methods, path, name)
