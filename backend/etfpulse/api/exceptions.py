"""Central registry of FastAPI exception handlers.

Anti-drift rule (D3): never use `@app.exception_handler(...)` in routes or
anywhere else. Every domain-to-HTTP mapping registers here so the
response-redaction policy stays auditable in one place.

Response bodies are deliberately opaque — callers see `{"detail": "..."}`
without any upstream internals. Details live in logs, not responses.

Handler-matching precedence (Starlette behaviour): more-specific subclasses
match before the base. So `SoSoValueMonthlyQuotaError` (subclass of
`SoSoValueError`) hits its own handler, not the base handler — we register
both.
"""

from __future__ import annotations

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from etfpulse.adapters.sosovalue import (
    SoSoValueError,
    SoSoValueMonthlyQuotaError,
    SoSoValueRateLimitError,
)

log = structlog.get_logger()


def register_exception_handlers(app: FastAPI) -> None:
    """Wire every domain-to-HTTP mapping. Call once in the app factory."""

    @app.exception_handler(SoSoValueMonthlyQuotaError)
    async def _quota_exceeded(request: Request, exc: SoSoValueMonthlyQuotaError) -> JSONResponse:
        log.error(
            "sosovalue_quota_exceeded",
            path=request.url.path,
            upstream_message=str(exc),
        )
        return JSONResponse(
            status_code=503,
            content={"detail": "upstream quota exceeded"},
        )

    @app.exception_handler(SoSoValueRateLimitError)
    async def _rate_limited(request: Request, exc: SoSoValueRateLimitError) -> JSONResponse:
        log.warning(
            "sosovalue_rate_limited",
            path=request.url.path,
            upstream_message=str(exc),
        )
        return JSONResponse(
            status_code=503,
            content={"detail": "upstream rate limited"},
        )

    @app.exception_handler(SoSoValueError)
    async def _sosovalue_generic(request: Request, exc: SoSoValueError) -> JSONResponse:
        log.error(
            "sosovalue_error",
            path=request.url.path,
            upstream_message=str(exc),
            exc_info=exc,
        )
        return JSONResponse(
            status_code=503,
            content={"detail": "upstream unavailable"},
        )

    @app.exception_handler(SQLAlchemyError)
    async def _db_error(request: Request, exc: SQLAlchemyError) -> JSONResponse:
        log.error(
            "database_error",
            path=request.url.path,
            exc_info=exc,
        )
        return JSONResponse(
            status_code=503,
            content={"detail": "database error"},
        )
