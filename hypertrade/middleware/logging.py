import logging
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp
from fastapi import Request

from ..config import get_settings
from ..security import _extract_client_ip


class LoggingMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp):
        super().__init__(app)
        self.logger = logging.getLogger("uvicorn.error")

    def _log_request(self, *, method: str, route: str, status: int, duration_ms: int, client_ip: str, req_id: str) -> None:
        self.logger.info(
            "%s %s -> %s %dms ip=%s req_id=%s",
            method,
            route,
            status,
            duration_ms,
            client_ip,
            req_id,
        )

    async def dispatch(self, request: Request, call_next):
        start = time.perf_counter()
        req_id = str(uuid.uuid4())
        settings = get_settings()

        client_ip = _extract_client_ip(request, settings.trust_forwarded_for) or "-"
        method = request.method
        path = request.url.path

        # Attach request id to request.state for downstream handlers
        try:
            request.state.request_id = req_id
        except Exception:
            pass

        response = None
        try:
            response = await call_next(request)
            return response
        finally:
            duration_ms = int((time.perf_counter() - start) * 1000)
            status = response.status_code if response else 500
            route = getattr(request.scope.get("route"), "path", path)
            self._log_request(
                method=method,
                route=route,
                status=status,
                duration_ms=duration_ms,
                client_ip=client_ip,
                req_id=req_id,
            )
            if response is not None:
                response.headers["X-Request-ID"] = req_id
                response.headers["X-Process-Time"] = f"{duration_ms}ms"
