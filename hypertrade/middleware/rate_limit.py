from __future__ import annotations

import time
import threading
from collections import deque
from typing import Deque, Dict, Optional, Iterable, Tuple, List

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from starlette.types import ASGIApp

from ..security import _extract_client_ip


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Simple per-IP sliding window rate limiter.

    - Limits requests per `window_seconds` with optional `burst`.
    - Respects `trust_forwarded_for` when extracting client IP.
    - Can target only specific `only_paths` and exclude `exclude_paths`.
    - Optionally allows a set of `whitelist_ips` to bypass limiting.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_requests: int,
        window_seconds: int,
        burst: int = 0,
        trust_forwarded_for: bool = True,
        only_paths: Optional[Iterable[str]] = None,
        exclude_paths: Optional[Iterable[str]] = None,
        whitelist_ips: Optional[Iterable[str]] = None,
    ):
        super().__init__(app)
        self.max_requests = max_requests
        self.window = window_seconds
        self.burst = burst
        self.trust_forwarded_for = trust_forwarded_for
        self.only_paths = set(only_paths or [])
        self.exclude_paths = set(exclude_paths or [])
        self.whitelist = set(whitelist_ips or [])

        # In-memory state: ip -> deque[timestamps]
        self._buckets: Dict[str, Deque[float]] = {}
        self._lock = threading.Lock()

    def _should_check(self, path: str) -> bool:
        if path in self.exclude_paths:
            return False
        if not self.only_paths:
            return True
        return path in self.only_paths

    def _prune_and_count(self, now: float, dq: Deque[float]) -> int:
        cutoff = now - self.window
        while dq and dq[0] <= cutoff:
            dq.popleft()
        return len(dq)

    def _allow(self, ip: str, now: float) -> Tuple[bool, int, float]:
        with self._lock:
            dq = self._buckets.get(ip)
            if dq is None:
                dq = deque()
                self._buckets[ip] = dq
            count = self._prune_and_count(now, dq)
            limit = self.max_requests + self.burst
            if count >= limit:
                # when will the window reset
                reset_in = max(0.0, (dq[0] + self.window) - now) if dq else self.window
                return False, max(0, limit - count), reset_in
            dq.append(now)
            remaining = max(0, limit - (count + 1))
            reset_in = max(0.0, (dq[0] + self.window) - now) if dq else self.window
            return True, remaining, reset_in

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if not self._should_check(path):
            return await call_next(request)

        ip = _extract_client_ip(request, self.trust_forwarded_for) or "-"
        if ip in self.whitelist:
            return await call_next(request)

        now = time.time()
        allowed, remaining, reset_in = self._allow(ip, now)
        if not allowed:
            return JSONResponse(
                status_code=429,
                content={"error": {"status": 429, "detail": "Too Many Requests"}},
                headers={
                    "Retry-After": str(int(reset_in)),
                    "X-RateLimit-Limit": str(self.max_requests + self.burst),
                    "X-RateLimit-Remaining": str(max(0, remaining)),
                    "X-RateLimit-Reset": str(int(reset_in)),
                },
            )

        # Pass through
        response = await call_next(request)
        try:
            limit_total = self.max_requests + self.burst
            response.headers.setdefault("X-RateLimit-Limit", str(limit_total))
            response.headers.setdefault("X-RateLimit-Remaining", str(max(0, remaining)))
            response.headers.setdefault("X-RateLimit-Reset", str(int(reset_in)))
        except Exception:
            pass
        return response

