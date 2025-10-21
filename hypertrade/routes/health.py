from fastapi import APIRouter
import logging

router = APIRouter(tags=["health"])

log = logging.getLogger("uvicorn.error")

@router.get("/health", summary="Health check")
def health() -> dict[str, str]:
    log.info("Health check OK")
    #  Simple health check endpoint
    #  it will be expanded checking hyperliquid connection
    return {"status": "ok"}

