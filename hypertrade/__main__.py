"""Module entrypoint: ``python -m hypertrade`` starts the daemon via uvicorn.

Importing :mod:`hypertrade.daemon` eagerly builds and validates the app — on
missing configuration it prints the startup banner and exits non-zero, exactly
like ``uvicorn hypertrade.daemon:app``. When config is valid, uvicorn serves the
app on the configured host/port (``HYPERTRADE_LISTEN_HOST`` /
``HYPERTRADE_LISTEN_PORT``, defaulting to ``0.0.0.0:6487``).
"""

import uvicorn

from .config import get_settings
from .daemon import app

# Fallback when HYPERTRADE_LISTEN_PORT is unset (Settings.listen_port is None).
DEFAULT_PORT = 6487
# Levels uvicorn accepts for its own logging config; anything else falls back.
_UVICORN_LOG_LEVELS = {"critical", "error", "warning", "info", "debug", "trace"}


def main() -> None:
    """Serve the Hypertrade daemon with uvicorn on the configured host/port."""
    settings = get_settings()
    level = settings.log_level.lower()
    uvicorn.run(
        app,
        host=settings.listen_host,
        port=settings.listen_port or DEFAULT_PORT,
        log_level=level if level in _UVICORN_LOG_LEVELS else "info",
    )


if __name__ == "__main__":
    main()
