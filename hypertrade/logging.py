"""Logging utilities: configure Uvicorn-compatible logs and startup banner."""

import logging as pylog
from logging import config as logging_config
from typing import Iterable, Optional, List
from fastapi.routing import APIRoute

from .version import __version__


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
    try:
        from uvicorn.config import LOGGING_CONFIG

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
    except Exception:
        # Fallback basic config
        pylog.basicConfig(level=numeric)

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
    whitelist_enabled: bool,
    whitelist_ips: Iterable[str],
    trust_xff: bool,
) -> None:
    """Log a startup banner with ASCII 'HYPERTRADE' and key settings."""
    logger = pylog.getLogger("uvicorn.error")
    listening = (
        f"http://{host}:{port}"
        if host is not None and port is not None
        else "uvicorn-configured address"
    )
    ascii_art = (
        "\n".join(
            [
                "#   # #   # ####  ##### ####  ##### ####   ###  ####  #####",
                "#   #  # #  #   # #     #   #   #   #   # #   # #   # #    ",
                "#####   #   ####  ####  ####    #   ####  ##### #   # #### ",
                "#   #   #   #     #     #  #    #   #  #  #   # #   # #    ",
                "#   #   #   #     ##### #   #   #   #   # #   # ####  #####",
            ]
        )
    )
    details = (
        f"Hypertrade Webhook Daemon v{__version__}\n"
        f"Listening: {listening}\n"
        f"Whitelist: {'ON' if whitelist_enabled else 'OFF'} ({len(list(whitelist_ips))} IPs)\n"
        f"Trust XFF: {'ON' if trust_xff else 'OFF'}"
    )
    ruler = "=" * 59
    banner = f"{ruler}\n{ascii_art}\n{ruler}\n{details}\n{ruler}"
    for line in banner.splitlines():
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
        logger.info("  %-7s %s", methods, path)
