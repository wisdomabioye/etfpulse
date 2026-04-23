"""FastAPI application entrypoint.

`create_app()` is the factory — tests call it to build isolated app instances
(no shared state between tests). The module-level `app = create_app()` is the
singleton uvicorn targets via `etfpulse.app:app`.

This file composes the api/ layer; it holds no business logic and imports
nothing from `models/`, `adapters/`, or `pipeline/` directly. All HTTP
concerns live in `etfpulse.api.*`.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware

from etfpulse.api.exceptions import register_exception_handlers
from etfpulse.api.lifespan import lifespan
from etfpulse.api.middleware.request_id import RequestIDMiddleware
from etfpulse.api.routes import ALL_ROUTERS
from etfpulse.config import Settings, settings


def create_app(settings_override: Settings | None = None) -> FastAPI:
    """Build a configured FastAPI instance.

    Re-entrant: every call returns a fresh app with no shared state. Tests
    should use this factory (often with `settings_override=...`) rather than
    importing the module-level `app`.
    """
    cfg = settings_override or settings

    app = FastAPI(
        title="ETFPulse",
        description="ETF flow signal intelligence",
        version="0.1.0",
        lifespan=lifespan,
        # Keep route paths unambiguous — Coolify/Caddy pass paths through as-is,
        # and implicit trailing-slash redirects cause broken CORS preflight.
        redirect_slashes=False,
    )

    # Middleware is added LIFO (last added = outermost). Final stack from
    # outermost → innermost: RequestID → CORS → Gzip → handler. Reasoning:
    # - Gzip innermost: it needs to see the raw non-streaming handler
    #   response so `minimum_size=500` works. If BaseHTTPMiddleware wraps
    #   the response (which RequestID does), Gzip sees a streaming response
    #   and compresses every chunk regardless of size — the 500-byte
    #   threshold becomes a no-op and every tiny response gets compressed.
    # - CORS wraps next: Access-Control-* headers don't care about body.
    # - RequestID outermost: binds request_id into structlog contextvars
    #   before ANY downstream middleware runs (including CORS preflight).
    #   The X-Request-ID header gets added on the way out, after compression.
    app.add_middleware(GZipMiddleware, minimum_size=500)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(RequestIDMiddleware)

    register_exception_handlers(app)

    for router in ALL_ROUTERS:
        app.include_router(router, prefix="/api")

    return app


app = create_app()
