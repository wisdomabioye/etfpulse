"""FastAPI dependency providers.

Anti-drift rule (D7): routes access shared resources through these deps, never
by importing singletons directly. Keeps routes unit-testable via
`app.dependency_overrides`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from etfpulse.config import settings
from etfpulse.db import async_session, engine


async def get_db_session() -> AsyncIterator[AsyncSession]:
    """Yield a DB session for the duration of a request.

    Usage: `session: AsyncSession = Depends(get_db_session)` on a route.
    Session is closed when the request ends; no per-request transaction
    management here — routes commit/rollback as they need to.
    """
    async with async_session() as session:
        yield session


def get_db_engine() -> AsyncEngine:
    """Return the shared async engine.

    Kept separate from `get_db_session` so the readiness probe can ping
    connectivity (`SELECT 1` on the engine) without paying for the session
    construction overhead on every probe.
    """
    return engine


async def require_admin_key(
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
) -> None:
    """Gate admin endpoints behind the `ADMIN_API_KEY` env var.

    When `settings.admin_api_key` is unset (empty string), admin endpoints are
    **disabled** — the dep returns 503. Admin access is opt-in: a real key must
    be configured in production for any admin route to respond.

    When the key IS configured, a missing or mismatched `X-Admin-Key` header
    returns 401.

    Usage on a route:
        `@router.post(..., dependencies=[Depends(require_admin_key)])`
    """
    if not settings.admin_api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="admin endpoints disabled",
        )
    if x_admin_key != settings.admin_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid admin key",
        )
