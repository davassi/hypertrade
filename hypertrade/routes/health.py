"""Simple health check endpoint."""

import logging
from fastapi import APIRouter

router = APIRouter(tags=["health"])

log = logging.getLogger("uvicorn.error")

@router.get("/health", summary="Health check")
def health() -> dict[str, str]:
    """Return a simple OK status for liveness probes."""
    log.info("Health check OK")
    #  Simple health check endpoint
    #  it will be expanded checking hyperliquid connection
    return {"status": "ok"}
