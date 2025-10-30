"""Middleware to enforce a maximum Content-Length on incoming requests."""

from __future__ import annotations

from fastapi import Request, HTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp


class ContentLengthLimitMiddleware(BaseHTTPMiddleware):
    """Reject requests exceeding ``max_bytes`` based on Content-Length header."""

    def __init__(self, app: ASGIApp, max_bytes: int):
        super().__init__(app)
        self.max_bytes = max_bytes

    async def dispatch(self, request: Request, call_next):
        """Validate Content-Length and short-circuit with 413 if too large."""
        cl = request.headers.get("content-length")
        if cl is not None:
            try:
                size = int(cl)
                if size > self.max_bytes:
                    raise HTTPException(status_code=413, detail="Payload too large")
            except ValueError:
                pass
        return await call_next(request)
