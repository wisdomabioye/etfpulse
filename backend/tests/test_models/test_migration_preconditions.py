"""Pin the precondition guards in the PR C.6 migration.

The migration `9e3f4e415e8d` ships four guard queries that abort the
upgrade BEFORE any structural change if the data would be silently
destroyed or would violate the new CHECK constraints:

  - `_assert_no_nondefault_tier()` — refuses to drop `tier` when any
    user/group row carries a non-default value (would be data loss).
  - `_assert_orders_venue_valid()` — refuses to add the `venue` CHECK
    when any orders row would fail it.
  - `_assert_positions_venue_valid()` — same for positions.
  - `_assert_orders_status_valid()` — refuses to swap the status CHECK
    when any orders row has an out-of-range status.

These guards are the *only* mechanism preventing silent data loss /
constraint violations at migration time. They are simple, but if a
future refactor changes table names, column names, or comparison
operators, the guard would silently always-pass and we'd lose the
safety net. This test file exercises the abort path for each guard.

Mechanics: each test downgrades the test DB to one revision before
C.6 (so the legacy schema with `tier`, etc. exists), seeds offending
data via raw SQL (bypassing model-level validation), runs the upgrade,
and asserts the migration aborts with the expected message. The
fixture's teardown removes the seed data and re-upgrades so subsequent
tests see a clean head.

Cost: ~2-3s per test (full downgrade + upgrade cycle). Trade-off
acknowledged — this is data-loss prevention, not a hot path.
"""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from etfpulse.config import settings

_ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"
_PRIOR_HEAD = "6ad6b15e7f00"  # last revision before C.6 — tier columns still exist


# Seed/cleanup runs through asyncpg (the only PG driver installed in this
# project). The tests themselves stay sync so they can call the sync
# `command.upgrade` / `downgrade` without event-loop coordination — each
# raw-SQL helper wraps a fresh `asyncio.run` to drive one short-lived
# async engine.
async def _exec_async(sql: str, params: dict) -> None:
    eng = create_async_engine(settings.database_url_test)
    try:
        async with eng.begin() as conn:
            await conn.execute(text(sql), params)
    finally:
        await eng.dispose()


async def _execscalar_async(sql: str, params: dict) -> object:
    eng = create_async_engine(settings.database_url_test)
    try:
        async with eng.begin() as conn:
            result = await conn.execute(text(sql), params)
            return result.scalar_one()
    finally:
        await eng.dispose()


def _exec(sql: str, **params) -> None:
    asyncio.run(_exec_async(sql, params))


def _execscalar(sql: str, **params) -> object:
    return asyncio.run(_execscalar_async(sql, params))


@pytest.fixture
def _at_prior_head() -> Generator[Config, None, None]:
    """Bring the test DB to the revision immediately before C.6 for the test,
    restore to head afterwards.

    `settings.database_url` is already set to `database_url_test` by the
    session-scoped autouse `_schema` fixture in conftest, so we do NOT swap
    it here — the alembic env.py reads from `settings.database_url` and we
    inherit the parent fixture's setup.

    Critical invariant: the test MUST clean up any seed rows before yielding
    back to teardown. If offending data remains, the post-test re-upgrade
    aborts (correctly) — but then every subsequent test in the session sees
    a half-migrated DB. The teardown catches and re-raises with a clear
    message so the failure is easy to diagnose.
    """
    cfg = Config(str(_ALEMBIC_INI))
    command.downgrade(cfg, _PRIOR_HEAD)
    try:
        yield cfg
    finally:
        try:
            command.upgrade(cfg, "head")
        except Exception as exc:
            raise RuntimeError(
                "Precondition-guard test left offending data in the DB; "
                "subsequent tests will see a half-migrated schema. "
                "Each test must clean up its seed before returning. "
                f"Underlying upgrade error: {exc}"
            ) from exc


def _seed_min_user_with_tier(*, user_id: int, tier: str) -> None:
    """Insert a User row at prior-head schema (when `tier` column exists)."""
    _exec(
        """
        INSERT INTO users (
            id, role, tier, agreed_at, is_active,
            pref_assets, pref_min_confidence, pref_paused, preferences
        ) VALUES (
            :id, 'user', :tier, NULL, true,
            ARRAY['BTC','ETH']::varchar(10)[], 5, false, '{}'
        )
        """,
        id=user_id,
        tier=tier,
    )


def _delete_user(user_id: int) -> None:
    _exec("DELETE FROM users WHERE id = :id", id=user_id)


def _seed_min_order(*, client_order_id: str, venue: str, status: str = "pending") -> None:
    """Insert an Order row at prior-head schema (no eip712 columns yet)."""
    _exec(
        """
        INSERT INTO orders (
            client_order_id, venue, asset, side, order_type, time_in_force,
            requested_size, status
        ) VALUES (
            :co, :venue, 'BTC', 'buy', 'limit', 'gtc',
            0.01, :status
        )
        """,
        co=client_order_id,
        venue=venue,
        status=status,
    )


def _delete_order(client_order_id: str) -> None:
    _exec("DELETE FROM orders WHERE client_order_id = :co", co=client_order_id)


def _seed_min_position(*, venue: str) -> int:
    """Insert a Position row at prior-head schema; return its id."""
    pid = _execscalar(
        """
        INSERT INTO positions (
            venue, asset, side, size, entry_price, status
        ) VALUES (
            :venue, 'BTC', 'long', 0.01, 65000, 'open'
        )
        RETURNING id
        """,
        venue=venue,
    )
    return int(pid)


def _delete_position(position_id: int) -> None:
    _exec("DELETE FROM positions WHERE id = :id", id=position_id)


# ---------------------------------------------------------------------------
# The four precondition guards
# ---------------------------------------------------------------------------


class TestTierGuard:
    """`_assert_no_nondefault_tier` — refuse to drop columns silently."""

    def test_aborts_when_user_has_nondefault_tier(self, _at_prior_head):
        cfg = _at_prior_head
        _seed_min_user_with_tier(user_id=999001, tier="premium")
        try:
            with pytest.raises(RuntimeError, match="non-default tier"):
                command.upgrade(cfg, "head")
        finally:
            _delete_user(999001)

    def test_aborts_when_group_has_nondefault_tier(self, _at_prior_head):
        cfg = _at_prior_head
        # Groups have their own tier column at prior head.
        _exec(
            """
            INSERT INTO telegram_groups (
                id, chat_id, title, tier, is_active,
                pref_assets, pref_min_confidence, pref_paused, preferences
            ) VALUES (
                999002, 999, 'g', 'enterprise', true,
                ARRAY['BTC','ETH']::varchar(10)[], 5, false, '{}'
            )
            """,
        )
        try:
            with pytest.raises(RuntimeError, match="non-default tier"):
                command.upgrade(cfg, "head")
        finally:
            _exec("DELETE FROM telegram_groups WHERE id = :id", id=999002)


class TestOrdersVenueGuard:
    """`_assert_orders_venue_valid` — refuse if a row would fail the new CHECK."""

    def test_aborts_on_legacy_sodex_value(self, _at_prior_head):
        """Prior to C.6, `venue` was unconstrained — any string was allowed.
        The legacy `'sodex'` literal would fail the new venue CHECK; the guard
        catches it before the constraint is even added."""
        cfg = _at_prior_head
        _seed_min_order(client_order_id="pg-orders-1", venue="sodex")
        try:
            with pytest.raises(RuntimeError, match="orders has .* with venue not in"):
                command.upgrade(cfg, "head")
        finally:
            _delete_order("pg-orders-1")


class TestPositionsVenueGuard:
    """`_assert_positions_venue_valid` — mirror for the Position table."""

    def test_aborts_on_legacy_sodex_value(self, _at_prior_head):
        cfg = _at_prior_head
        pid = _seed_min_position(venue="sodex")
        try:
            with pytest.raises(RuntimeError, match="positions has .* with venue not in"):
                command.upgrade(cfg, "head")
        finally:
            _delete_position(pid)


class TestOrdersStatusGuard:
    """`_assert_orders_status_valid` — the old 5-status CHECK was a subset
    of the new 8, so historical data passes by construction. But if a future
    code path ever wrote a non-enum status, the guard catches it before the
    CHECK swap renders the row invalid."""

    def test_aborts_on_unknown_status(self, _at_prior_head):
        cfg = _at_prior_head
        # The OLD status CHECK already restricts to 5 values, so we can't
        # naively INSERT 'unknown'. Drop the constraint, insert, then run
        # upgrade — this simulates a corrupted DB state.
        _exec("ALTER TABLE orders DROP CONSTRAINT ck_orders_status_enum")
        _seed_min_order(client_order_id="pg-status-1", venue="sodex_spot", status="unknown")
        try:
            with pytest.raises(RuntimeError, match="orders has .* with status outside"):
                command.upgrade(cfg, "head")
        finally:
            _delete_order("pg-status-1")
            # Restore the constraint so the fixture's re-upgrade has a clean
            # starting state (alembic's upgrade will re-DROP + re-CREATE).
            _exec(
                "ALTER TABLE orders ADD CONSTRAINT ck_orders_status_enum "
                "CHECK (status IN ('pending','partially_filled','filled',"
                "'cancelled','rejected'))"
            )
