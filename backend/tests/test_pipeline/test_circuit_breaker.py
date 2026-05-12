"""CircuitBreaker persistence (issue #65) — record / resolve / count_active."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from etfpulse.models.regime import CircuitBreaker, CircuitBreakerTrigger
from etfpulse.pipeline import circuit_breaker


class TestRecord:
    async def test_first_call_inserts_row(self, db_session):
        row = await circuit_breaker.record(
            db_session,
            CircuitBreakerTrigger.MACRO_EVENT.value,
            details={"event": "FOMC"},
        )
        assert row is not None
        assert row.id is not None
        assert row.trigger_type == "macro_event"
        assert row.resolved_at is None
        assert row.details == {"event": "FOMC"}

    async def test_idempotent_while_unresolved(self, db_session):
        first = await circuit_breaker.record(
            db_session, CircuitBreakerTrigger.MANUAL.value, details={"by": "ops"}
        )
        assert first is not None
        second = await circuit_breaker.record(
            db_session, CircuitBreakerTrigger.MANUAL.value, details={"by": "different"}
        )
        # Same trigger_type already unresolved → no new row.
        assert second is None
        # Original row's details unchanged.
        await db_session.refresh(first)
        assert first.details == {"by": "ops"}

    async def test_different_trigger_types_coexist(self, db_session):
        a = await circuit_breaker.record(db_session, CircuitBreakerTrigger.MACRO_EVENT.value)
        b = await circuit_breaker.record(db_session, CircuitBreakerTrigger.MANUAL.value)
        assert a is not None and b is not None
        assert a.id != b.id

    async def test_re_record_after_resolve(self, db_session):
        first = await circuit_breaker.record(db_session, CircuitBreakerTrigger.MANUAL.value)
        assert first is not None
        await circuit_breaker.resolve(
            db_session, CircuitBreakerTrigger.MANUAL.value, resolved_by="auto"
        )
        # Once resolved, a new trip can record a fresh row.
        second = await circuit_breaker.record(db_session, CircuitBreakerTrigger.MANUAL.value)
        assert second is not None
        assert second.id != first.id


class TestResolve:
    async def test_returns_zero_when_nothing_active(self, db_session):
        rowcount = await circuit_breaker.resolve(
            db_session, CircuitBreakerTrigger.MANUAL.value, resolved_by="auto"
        )
        assert rowcount == 0

    async def test_flips_resolved_at(self, db_session):
        row = await circuit_breaker.record(db_session, CircuitBreakerTrigger.MANUAL.value)
        assert row is not None
        rowcount = await circuit_breaker.resolve(
            db_session, CircuitBreakerTrigger.MANUAL.value, resolved_by="ops"
        )
        assert rowcount == 1
        await db_session.refresh(row)
        assert row.resolved_at is not None
        assert row.resolved_by == "ops"


class TestCountActive:
    async def test_empty(self, db_session):
        assert await circuit_breaker.count_active(db_session) == 0

    async def test_counts_only_unresolved(self, db_session):
        await circuit_breaker.record(db_session, CircuitBreakerTrigger.MACRO_EVENT.value)
        await circuit_breaker.record(db_session, CircuitBreakerTrigger.MANUAL.value)
        assert await circuit_breaker.count_active(db_session) == 2

        await circuit_breaker.resolve(
            db_session, CircuitBreakerTrigger.MACRO_EVENT.value, resolved_by="auto"
        )
        assert await circuit_breaker.count_active(db_session) == 1


class TestCheckConstraint:
    async def test_rejects_unknown_trigger_type(self, db_session):
        # CHECK ck_circuit_breakers_trigger_type_enum (added in PR C.1)
        # should reject anything outside the StrEnum's values.
        with pytest.raises(Exception):  # IntegrityError subclasses
            await circuit_breaker.record(db_session, "definitely-not-a-real-trigger")

    async def test_check_against_orm_path(self, db_session):
        # Belt and braces — same constraint via a raw ORM add (bypass our
        # record() helper) to prove the DB CHECK fires regardless of how
        # the row is created.
        row = CircuitBreaker(trigger_type="bogus")
        db_session.add(row)
        with pytest.raises(Exception):
            await db_session.flush()
        await db_session.rollback()
        # Sanity: known-good value still passes
        good = CircuitBreaker(trigger_type=CircuitBreakerTrigger.MANUAL.value)
        db_session.add(good)
        await db_session.flush()
        found = (
            await db_session.execute(select(CircuitBreaker).where(CircuitBreaker.id == good.id))
        ).scalar_one()
        assert found.trigger_type == "manual"
