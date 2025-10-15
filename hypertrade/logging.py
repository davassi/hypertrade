import logging
import logging.config
from typing import Iterable, Optional
from fastapi.routing import APIRoute

from .version import __version__

def setup_logging(level: str = "INFO") -> None:
    """Configure logging to use Uvicorn's format when possible.

    - If Uvicorn hasn't configured logging (e.g., running `python -m app`), apply
      Uvicorn's default LOGGING_CONFIG so our logs match its format.
    - Otherwise, only adjust levels so our loggers integrate with Uvicorn's handlers.
    """
    numeric = getattr(logging, level, logging.INFO)
    try:
        from uvicorn.config import LOGGING_CONFIG

        root = logging.getLogger()
        if not root.handlers:
            cfg = LOGGING_CONFIG.copy()
            loggers_cfg = cfg.get("loggers", {})
            if "uvicorn.error" in loggers_cfg:
                loggers_cfg["uvicorn.error"]["level"] = level
            if "uvicorn.access" in loggers_cfg:
                loggers_cfg["uvicorn.access"]["level"] = level
            logging.config.dictConfig(cfg)
    except Exception:
        # Fallback basic config
        logging.basicConfig(level=numeric)

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).setLevel(numeric)

# Log a startup banner with key settings
def log_startup_banner(
    *,
    host: Optional[str] = None,
    port: Optional[int] = None,
    whitelist_enabled: bool,
    whitelist_ips: Iterable[str],
    trust_xff: bool,
) -> None:
    logger = logging.getLogger("uvicorn.error")
    listening = f"http://{host}:{port}" if host and port else "uvicorn-configured address"
    banner = f"""
============================================================
  HyperTrade Webhook Daemon v{__version__}
  Listening: {listening}
  Whitelist: {'ON' if whitelist_enabled else 'OFF'} ({len(list(whitelist_ips))} IPs)
  Trust XFF: {'ON' if trust_xff else 'OFF'}
============================================================
""".strip("\n")
    for line in banner.splitlines():
        logger.info(line)

# Log all registered endpoints in the app
def log_endpoints(app) -> None:
    logger = logging.getLogger("uvicorn.error")
    lines = []
    for route in getattr(app, "routes", []):
        if isinstance(route, APIRoute):
            methods = sorted(m for m in (route.methods or []) if m not in {"HEAD", "OPTIONS"})
            method_str = ",".join(methods) or "-"
            lines.append((route.path, method_str, route.name))
    lines.sort(key=lambda x: (x[0], x[1]))
    header = f"Available endpoints ({len(lines)}):"
    logger.info("%s", header)
    for path, methods, name in lines:
        logger.info("  %-7s %s", methods, path)
