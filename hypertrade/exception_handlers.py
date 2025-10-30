"""Exception handlers for FastAPI app with concise JSON responses."""

import logging as pylog
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.encoders import jsonable_encoder
from starlette.exceptions import HTTPException as StarletteHTTPException

log = pylog.getLogger("uvicorn.error")

def _extract_request_id(response_headers: Optional[dict[str, str]]) -> Optional[str]:
    if not response_headers:
        return None
    return response_headers.get("X-Request-ID")

async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    """Handle Starlette HTTP exceptions uniformly."""
    req_id = getattr(request.state, "request_id", None)
    # Optionally suppress noisy 404 logs from scans
    try:
        settings = request.app.state.settings
        suppress_404 = getattr(settings, "suppress_404_logs", False)
    except AttributeError:
        suppress_404 = False

    if not (suppress_404 and exc.status_code == 404):
        log.warning(
            "HTTPException %s %s -> %s req_id=%s",
            request.method,
            request.url.path,
            exc.status_code,
            req_id,
        )
    content = {
        "error": {
            "status": exc.status_code,
            "detail": exc.detail,
            "path": request.url.path,
            "request_id": req_id,
        }
    }
    # Use jsonable_encoder to avoid bytes/non-serializable types breaking dumps
    return JSONResponse(
        status_code=exc.status_code,
        content=jsonable_encoder(content),
        headers={"X-Request-ID": req_id} if req_id else None,
    )

async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Handle FastAPI validation errors with minimal noise."""
    req_id = getattr(request.state, "request_id", None)
    # Do not log stack traces for validation errors; keep concise
    log.warning(
        "ValidationError %s %s -> 422 req_id=%s errors=%s",
        request.method,
        request.url.path,
        req_id,
        exc.errors(),
    )
    content = {
        "error": {
            "status": 422,
            "detail": "Request validation failed",
            "errors": exc.errors(),
            "path": request.url.path,
            "request_id": req_id,
        }
    }
    # Some validation contexts include bytes (e.g., raw body); encode safely
    return JSONResponse(
        status_code=422,
        content=jsonable_encoder(content),
        headers={"X-Request-ID": req_id} if req_id else None,
    )

async def unhandled_exception_handler(request: Request, exc: Exception):
    """Catch-all handler for unexpected exceptions."""
    req_id = getattr(request.state, "request_id", None)
    # Avoid stacktraces; summarize error type and request id
    log.error(
        "UnhandledError %s %s -> 500 err=%s req_id=%s",
        request.method,
        request.url.path,
        exc.__class__.__name__,
        req_id,
    )
    content = {
        "error": {
            "status": 500,
            "detail": "Internal server error",
            "path": request.url.path,
            "request_id": req_id,
        }
    }
    return JSONResponse(
        status_code=500,
        content=jsonable_encoder(content),
        headers={"X-Request-ID": req_id} if req_id else None,
    )

def register_exception_handlers(app: FastAPI) -> None:
    """Register all custom exception handlers on the app."""
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
