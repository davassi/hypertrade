from fastapi import APIRouter
import logging

router = APIRouter(tags=["health"])

log = logging.getLogger("uvicorn.error")

@router.get("/health", summary="Health check")
def health() -> dict[str, str]:
    log.info("Health check OK")
    return {"status": "ok"}

