import logging
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger("uvicorn.error")

def _extract_request_id(response_headers: Optional[dict[str, str]]) -> Optional[str]:
    if not response_headers:
        return None
    return response_headers.get("X-Request-ID")

async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    log.warning("HTTPException %s %s -> %s", request.method, request.url.path, exc.status_code)
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "status": exc.status_code,
                "detail": exc.detail,
                "path": request.url.path,
            }
        },
    )

async def validation_exception_handler(request: Request, exc: RequestValidationError):
    log.warning("ValidationError on %s %s: %s", request.method, request.url.path, exc.errors())
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "status": 422,
                "detail": "Request validation failed",
                "errors": exc.errors(),
                "path": request.url.path,
            }
        },
    )

async def unhandled_exception_handler(request: Request, exc: Exception):
    log.exception("Unhandled exception on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "status": 500,
                "detail": "Internal server error",
                "path": request.url.path,
            }
        },
    )

def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
