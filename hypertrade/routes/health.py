"""Health check endpoints for liveness and readiness probes."""

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from ..config import get_settings
from .hyperliquid_service import HyperliquidService

router = APIRouter(tags=["health"])

log = logging.getLogger("uvicorn.error")


@router.get("/health", summary="Liveness probe")
def health() -> dict[str, str]:
    """Liveness probe: simple uptime check. Always returns 200 if service is running.

    Use this for Kubernetes liveness probes to detect hung processes.
    """
    log.debug("Liveness check")
    return {"status": "alive"}


@router.get("/ready", summary="Readiness probe")
def readiness() -> dict:
    """Readiness probe: verifies service is ready to handle requests.

    Checks:
    - Hyperliquid API is accessible 
    - Credentials are valid 

    Returns:
        {"status": "ready", "available_balance": <float>} on success

    Raises:
        HTTPException(503): If Hyperliquid is unreachable or credentials invalid
    """
    log.debug("Readiness check requested.")

    # Verify Hyperliquid connectivity
    try:
        settings = get_settings()
        client = HyperliquidService(
            base_url=settings.api_url,
            master_addr=settings.master_addr,
            api_wallet_priv=settings.api_wallet_priv.get_secret_value(),
            subaccount_addr=settings.subaccount_addr,
        )

        # Test connectivity by fetching available balance
        # This validates both API accessibility and credential validity
        balance = client.client.data.get_available_balance()
        log.info("Readiness check passed: Hyperliquid connected, balance=%.2f USDC", balance)
        return {"status": "ready", "available_balance": balance}

    except Exception as e:
        log.error("Readiness check failed: Hyperliquid unavailable: %s", str(e))
        raise HTTPException(
            status_code=503,
            detail=f"Service not ready: Hyperliquid connection failed - {str(e)}"
        ) from e
