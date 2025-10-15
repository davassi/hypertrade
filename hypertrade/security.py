from typing import Iterable

from fastapi import Depends, HTTPException, Request

from .config import get_settings

# Extract client IP from request, considering X-Forwarded-For if trusted
def _extract_client_ip(request: Request, trust_forwarded_for: bool) -> str | None:
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

# Dependency to enforce IP whitelist on routes
def require_ip_whitelisted(allowed_ips: Iterable[str] | None = None):
    async def dependency(request: Request, settings=Depends(get_settings)):
        if not settings.ip_whitelist_enabled:
            return  # whitelist disabled; allow

        ips = set(allowed_ips or settings.tv_webhook_ips or [])
        client_ip = _extract_client_ip(request, settings.trust_forwarded_for)
        if client_ip is None or client_ip not in ips:
            raise HTTPException(status_code=403, detail="Forbidden: IP not allowed")

    return dependency

