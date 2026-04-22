"""Request-ID middleware.

Accepts an inbound `X-Request-ID` header or generates a UUID4. Binds the ID
into structlog's context vars so every log line emitted during the request
carries `request_id=...` for correlation. Echoes the header on the response
so clients can correlate on their side.

Clears contextvars at the start of every request — ASGI workers are long-lived
and without clearing, a prior request's IDs could bleed through.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

import structlog
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

HEADER_NAME = "X-Request-ID"


class RequestIDMiddleware(BaseHTTPMiddleware):
    """See module docstring."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        request_id = request.headers.get(HEADER_NAME) or str(uuid.uuid4())

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            request_id=request_id,
            method=request.method,
            path=request.url.path,
        )

        response = await call_next(request)
        response.headers[HEADER_NAME] = request_id
        return response
