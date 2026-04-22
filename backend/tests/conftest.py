"""Test harness.

- Schema is built once per session by running `alembic upgrade head` against
  `settings.database_url_test`, and dropped via `alembic downgrade base` at
  teardown. Using migrations (not `Base.metadata.create_all`) means CI catches
  any drift between models and migrations on every run — if you add a column
  without a migration, tests fail.
- Each test runs on its own connection wrapped in a transaction that rolls
  back at exit, so tests are isolated without paying for upgrade per test.
- SOSOVALUE_USE_FIXTURES is forced on so the adapter never hits the network.
  Instance caches on `sosovalue_client` are cleared before each test so cached
  fixture responses don't leak between cases.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from etfpulse.adapters.sosovalue import sosovalue_client
from etfpulse.config import settings

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
_ALEMBIC_INI = _BACKEND_ROOT / "alembic.ini"


@pytest.fixture(scope="session", autouse=True)
def _force_fixture_mode() -> None:
    """Make the SoSoValue adapter serve fixtures for every test."""
    settings.sosovalue_use_fixtures = True


@pytest.fixture(scope="session", autouse=True)
def _schema():
    """Bring the test DB up to head via Alembic, tear back to base after.

    We point Alembic at the test DB by temporarily swapping `settings.database_url`
    — env.py reads from settings.database_url, so this is the cleanest override.
    Runs serially via Alembic's own `asyncio.run` inside env.py, so this is a
    plain (sync) fixture — no event-loop coordination needed.
    """
    original_url = settings.database_url
    settings.database_url = settings.database_url_test
    try:
        cfg = Config(str(_ALEMBIC_INI))
        # Idempotent reset in case a prior test run crashed mid-schema.
        command.downgrade(cfg, "base")
        command.upgrade(cfg, "head")
        yield
        command.downgrade(cfg, "base")
    finally:
        settings.database_url = original_url


@pytest.fixture(scope="session")
async def test_engine():
    """Session-scoped async engine bound to the test DB."""
    engine = create_async_engine(settings.database_url_test, pool_pre_ping=True)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def db_session(test_engine) -> AsyncGenerator[AsyncSession, None]:
    """Per-test session wrapped in a transaction rolled back on exit."""
    async with test_engine.connect() as conn:
        trans = await conn.begin()
        session = AsyncSession(bind=conn, expire_on_commit=False)
        try:
            yield session
        finally:
            await session.close()
            await trans.rollback()


@pytest.fixture(autouse=True)
def _clear_sosovalue_caches() -> None:
    """Reset adapter TTL caches between tests to avoid fixture bleed-through."""
    sosovalue_client._flows_cache.clear()
    sosovalue_client._news_cache.clear()
    sosovalue_client._macro_cache.clear()
