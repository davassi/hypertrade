"""Route security helpers: IP whitelisting and client IP extraction."""

from typing import Iterable, Optional

from fastapi import Depends, HTTPException, Request

from .config import get_settings


def _extract_client_ip(request: Request, trust_forwarded_for: bool) -> Optional[str]:
    """Return best-effort client IP.

    If ``trust_forwarded_for`` is true and an ``X-Forwarded-For`` header is present,
    use its left-most value. Otherwise, fall back to the socket peer address.
    """
    if trust_forwarded_for:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            # format: client, proxy1, proxy2 ... take left-most
            first = xff.split(",")[0].strip()
            if first:
                return first
    if request.client:
        return request.client.host
    return None

def require_ip_whitelisted(allowed_ips: Optional[Iterable[str]] = None):
    """FastAPI dependency that enforces a simple IP whitelist.

    If app settings have ``ip_whitelist_enabled`` on, only requests from ``allowed_ips``
    (or ``settings.tv_webhook_ips`` when not provided) are permitted.
    """
    async def dependency(request: Request, settings=Depends(get_settings)):
        if not settings.ip_whitelist_enabled:
            return  # whitelist disabled; allow

        ips = set(allowed_ips or settings.tv_webhook_ips or [])
        client_ip = _extract_client_ip(request, settings.trust_forwarded_for)
        if client_ip is None or client_ip not in ips:
            raise HTTPException(status_code=403, detail="Forbidden: IP not allowed")

    return dependency
