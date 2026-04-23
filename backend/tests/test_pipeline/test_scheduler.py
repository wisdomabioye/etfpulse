"""Scheduler module tests.

Two layers:
    - `_needs_catchup` is exercised against the real test DB via `db_session`
      (the catch-up decision logic is the non-trivial part).
    - `start_scheduler` orchestration is exercised with `_needs_catchup` and
      `_run_cycle_with_session` mocked, so we test wiring without paying for
      a full cycle on every test.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi import FastAPI

from etfpulse.config import settings
from etfpulse.models import ETFFlow
from etfpulse.pipeline.scheduler import _needs_catchup, start_scheduler

# ---------------------------------------------------------------------------
# _needs_catchup against real DB
# ---------------------------------------------------------------------------


class TestNeedsCatchup:
    async def test_empty_table_needs_catchup(self, db_session):
        assert await _needs_catchup(db_session) is True

    async def test_today_no_catchup(self, db_session):
        # Same-day data is impossible from EOD-publishing SoSoValue, but if
        # we ever have it, no catch-up is needed.
        db_session.add(
            ETFFlow(
                asset="BTC",
                captured_at=datetime.now(UTC).date(),
                total_net_flow_usd=Decimal("100"),
            )
        )
        await db_session.flush()
        assert await _needs_catchup(db_session) is False

    async def test_yesterday_no_catchup(self, db_session):
        # Steady state — last cron at 04:30 UTC ingested yesterday's EOD data.
        # On a fresh boot today between 00:00 and 04:30 UTC, latest = yesterday.
        # Catch-up MUST NOT fire here or it'd run on every routine boot.
        db_session.add(
            ETFFlow(
                asset="BTC",
                captured_at=datetime.now(UTC).date() - timedelta(days=1),
                total_net_flow_usd=Decimal("100"),
            )
        )
        await db_session.flush()
        assert await _needs_catchup(db_session) is False

    async def test_two_days_old_needs_catchup(self, db_session):
        # We missed at least one cron — catch-up self-heals.
        db_session.add(
            ETFFlow(
                asset="BTC",
                captured_at=datetime.now(UTC).date() - timedelta(days=2),
                total_net_flow_usd=Decimal("100"),
            )
        )
        await db_session.flush()
        assert await _needs_catchup(db_session) is True


# ---------------------------------------------------------------------------
# start_scheduler orchestration
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_cycle(monkeypatch):
    """Replace the real cycle wrapper with a no-op so tests don't touch the DB."""
    called = []

    async def _stub() -> None:
        called.append(datetime.now(UTC))

    monkeypatch.setattr("etfpulse.pipeline.scheduler._run_cycle_with_session", _stub)
    return called


@pytest.fixture
def stub_no_catchup(monkeypatch):
    """Make `_needs_catchup` return False without hitting the DB."""

    async def _stub(session) -> bool:
        return False

    monkeypatch.setattr("etfpulse.pipeline.scheduler._needs_catchup", _stub)


@pytest.fixture
def stub_needs_catchup(monkeypatch):
    """Make `_needs_catchup` return True without hitting the DB."""

    async def _stub(session) -> bool:
        return True

    monkeypatch.setattr("etfpulse.pipeline.scheduler._needs_catchup", _stub)


class TestStartScheduler:
    async def test_run_scheduler_false_yields_no_jobs(self, monkeypatch):
        """Case (a): R12 — disabled scheduler is a no-op contextmanager."""
        monkeypatch.setattr(settings, "run_scheduler", False)
        app = FastAPI()
        async with start_scheduler(app):
            assert not hasattr(app.state, "scheduler"), (
                "scheduler must NOT be attached when disabled"
            )

    async def test_enabled_registers_daily_cron(self, monkeypatch, stub_cycle, stub_no_catchup):
        monkeypatch.setattr(settings, "run_scheduler", True)
        app = FastAPI()
        async with start_scheduler(app):
            jobs = app.state.scheduler.get_jobs()
            assert any(j.id == "daily_cycle" for j in jobs)
            assert not any(j.id == "catchup" for j in jobs)

    async def test_fresh_db_triggers_catchup_job(self, monkeypatch, stub_cycle, stub_needs_catchup):
        """Case (b): catch-up needed → DateTrigger job appears alongside the cron."""
        monkeypatch.setattr(settings, "run_scheduler", True)
        app = FastAPI()
        async with start_scheduler(app):
            jobs = {j.id: j for j in app.state.scheduler.get_jobs()}
            assert "daily_cycle" in jobs
            assert "catchup" in jobs

    async def test_recent_data_skips_catchup(self, monkeypatch, stub_cycle, stub_no_catchup):
        """Case (c): catch-up not needed → only the cron job is registered."""
        monkeypatch.setattr(settings, "run_scheduler", True)
        app = FastAPI()
        async with start_scheduler(app):
            jobs = {j.id for j in app.state.scheduler.get_jobs()}
            assert "daily_cycle" in jobs
            assert "catchup" not in jobs

    async def test_shutdown_under_5_seconds(self, monkeypatch, stub_cycle, stub_no_catchup):
        """Case (d): teardown latency must be near-instant — Coolify deploys
        time out fast, and we never want to be the slow tenant."""
        monkeypatch.setattr(settings, "run_scheduler", True)
        app = FastAPI()

        start = time.monotonic()
        async with start_scheduler(app):
            pass  # immediately exit
        elapsed = time.monotonic() - start

        assert elapsed < 5.0, f"shutdown took {elapsed:.2f}s — should be sub-second"

    async def test_cron_uses_configured_hour_minute(self, monkeypatch, stub_cycle, stub_no_catchup):
        monkeypatch.setattr(settings, "run_scheduler", True)
        monkeypatch.setattr(settings, "scheduler_cron_hour", 7)
        monkeypatch.setattr(settings, "scheduler_cron_minute", 15)

        app = FastAPI()
        async with start_scheduler(app):
            cron_job = app.state.scheduler.get_job("daily_cycle")
            assert cron_job is not None
            # CronTrigger fields are stored as a list of named field objects.
            fields = {f.name: str(f) for f in cron_job.trigger.fields}
            assert fields["hour"] == "7"
            assert fields["minute"] == "15"

    async def test_cron_timezone_is_utc(self, monkeypatch, stub_cycle, stub_no_catchup):
        """Issue #31 — cron must be UTC-pinned regardless of host TZ."""
        monkeypatch.setattr(settings, "run_scheduler", True)
        app = FastAPI()
        async with start_scheduler(app):
            cron_job = app.state.scheduler.get_job("daily_cycle")
            assert cron_job is not None
            tz = cron_job.trigger.timezone
            # Different APScheduler versions return zoneinfo.ZoneInfo or
            # pytz.UTC — both stringify cleanly.
            assert "UTC" in str(tz).upper() or str(tz) == "UTC"
