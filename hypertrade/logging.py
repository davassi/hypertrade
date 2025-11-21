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
log = logging.getLogger("uvicorn.error")

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

def log_startup_banner(
    *,
    host: Optional[str] = None,
    port: Optional[int] = None,
    whitelist_enabled: bool = False,
    whitelist_ips: Iterable[str] = (),
    trust_xff: bool = True,
    version: str = "1.0.0",  # pass __version__ or from importlib.metadata
) -> None:
   
    url = f"http://{host}:{port}"
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
    log = pylog.getLogger("uvicorn.error")
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
