"""Structured logging setup.

One-time configuration of structlog + stdlib logging. Called from the FastAPI
lifespan at startup. Idempotent so tests building multiple apps don't trip
over repeated calls.

Prod emits JSON (one line per event — Coolify/Loki-friendly). Dev emits
`ConsoleRenderer` output (colorised, readable).
"""

from __future__ import annotations

import logging
import sys

import structlog

from etfpulse.config import settings


def configure_logging() -> None:
    """Configure structlog + stdlib logging once per process.

    Safe to call multiple times — structlog's `cache_logger_on_first_use`
    means re-calling only reconfigures the processor chain, not the logger
    instances already bound.
    """
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.stdlib.add_log_level,
        structlog.processors.StackInfoRenderer(),
    ]

    if settings.is_production:
        renderer: structlog.types.Processor = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer()

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # Align stdlib logging with structlog's level gate so libraries (httpx,
    # uvicorn, sqlalchemy) respect `LOG_LEVEL`.
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        force=True,  # reconfigure if basicConfig was called earlier
    )
