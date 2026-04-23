"""FastAPI dependency providers.

Anti-drift rule (D7): routes access shared resources through these deps, never
by importing singletons directly. Keeps routes unit-testable via
`app.dependency_overrides`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Header, HTTPException, Request, status
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


# ---------------------------------------------------------------------------
# Telegram webhook gates — used together as an ordered chain. Order matters
# (scanner-visibility), so routes declare them in `dependencies=[]` list in
# this exact sequence: suffix → bot-enabled → secret.
# ---------------------------------------------------------------------------


async def verify_webhook_suffix(suffix: str) -> None:
    """404 on suffix mismatch.

    First in the chain so random scanners get "route doesn't exist" behaviour,
    indistinguishable from any other unknown URL. FastAPI path parameters are
    shared between route and dep by name — `suffix` here is the same value
    that lands in the route handler.
    """
    expected = settings.telegram_webhook_url_suffix
    # Empty expected should also 404 (bot disabled state — defence in depth
    # for the `verify_bot_enabled` dep which is the authoritative check).
    if not expected or suffix != expected:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)


def verify_bot_enabled(request: Request) -> None:
    """404 if bot is disabled / Application not attached to app.state.

    Returns 404 (not 503) so scanners cannot distinguish "bot disabled" from
    "route does not exist" — same info leakage principle as `verify_webhook_suffix`.
    Runs AFTER suffix check so correct-suffix-but-bot-off still looks like a
    plain 404.
    """
    if getattr(request.app.state, "bot_application", None) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)


async def verify_telegram_secret(
    x_telegram_bot_api_secret_token: str | None = Header(
        default=None, alias="X-Telegram-Bot-Api-Secret-Token"
    ),
) -> None:
    """401 on secret mismatch.

    Last in the chain — only reachable once suffix and bot-enabled gates pass.
    Telegram sends this header on every legitimate webhook POST when we
    register via `set_webhook(secret_token=...)`. An empty configured secret
    also rejects defensively, though `verify_bot_enabled` should have caught
    the disabled-bot case already.
    """
    expected = settings.telegram_webhook_secret
    if not expected or x_telegram_bot_api_secret_token != expected:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
