"""Health probes.

Two endpoints with different failure semantics:

- `/api/health` (liveness) — returns 200 unconditionally. If the process is
  running and responding, it's alive. A 500 here means Coolify/k8s should
  restart the container. We do NOT check the DB here — transient DB problems
  must not cause restart loops.

- `/api/health/ready` (readiness) — 200 only when the DB is reachable within
  2s. Failure returns 503 so the load balancer takes us out of rotation
  without killing the container. Correct behaviour for a DB blip: stop
  serving traffic, recover when DB recovers.
"""

from __future__ import annotations

import asyncio

import structlog
from fastapi import APIRouter, Depends, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from etfpulse.api.deps import get_db_engine

log = structlog.get_logger()
router = APIRouter(tags=["health"])

_DB_PING_TIMEOUT_SECONDS = 2.0


@router.api_route(
    "/health",
    methods=["GET", "HEAD"],
    include_in_schema=False,  # infrastructure probe, not part of product API
)
async def liveness() -> dict[str, str]:
    """Accepts HEAD too — some orchestrators (Coolify with certain probes,
    kube-proxy health checks) use HEAD to avoid a response body."""
    return {"status": "ok"}


@router.api_route(
    "/health/ready",
    methods=["GET", "HEAD"],
    include_in_schema=False,
)
async def readiness(
    response: Response,
    engine: AsyncEngine = Depends(get_db_engine),
) -> dict[str, str]:
    if await _ping_db(engine):
        return {"status": "ok", "db": "ok"}
    response.status_code = 503
    return {"status": "degraded", "db": "error"}


async def _ping_db(engine: AsyncEngine) -> bool:
    """Return True if the DB accepts a trivial SELECT within the probe budget."""
    try:
        async with asyncio.timeout(_DB_PING_TIMEOUT_SECONDS):
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:
        log.warning("readiness_db_ping_failed", error=str(exc))
        return False
