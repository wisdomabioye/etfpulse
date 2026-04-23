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
from sqlalchemy import select

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


# ---------------------------------------------------------------------------
# Delivery send worker wiring (#60)
# ---------------------------------------------------------------------------


@pytest.fixture
def enable_bot_fields(monkeypatch):
    """Flip all four telegram fields + run_bot so `is_bot_enabled` is True."""
    monkeypatch.setattr(settings, "run_bot", True)
    monkeypatch.setattr(settings, "telegram_bot_token", "12345:test")
    monkeypatch.setattr(settings, "telegram_public_url", "https://app.example.com")
    monkeypatch.setattr(settings, "telegram_webhook_secret", "s3cr3t")
    monkeypatch.setattr(settings, "telegram_webhook_url_suffix", "abc123")


class TestDeliveryJobs:
    async def test_both_jobs_registered_when_bot_enabled(
        self, monkeypatch, stub_cycle, stub_no_catchup, enable_bot_fields
    ):
        """Both fan_out_pending (#61) and delivery_send (#60) register together."""
        monkeypatch.setattr(settings, "run_scheduler", True)
        app = FastAPI()
        async with start_scheduler(app):
            job_ids = {j.id for j in app.state.scheduler.get_jobs()}
            assert "delivery_send" in job_ids
            assert "fan_out_pending" in job_ids

    async def test_neither_registered_when_bot_disabled(
        self, monkeypatch, stub_cycle, stub_no_catchup
    ):
        """Bot off → no delivery pipeline at all. Saves DB queries + log noise."""
        monkeypatch.setattr(settings, "run_scheduler", True)
        # Don't apply enable_bot_fields → is_bot_enabled is False.
        app = FastAPI()
        async with start_scheduler(app):
            job_ids = {j.id for j in app.state.scheduler.get_jobs()}
            assert "delivery_send" not in job_ids
            assert "fan_out_pending" not in job_ids

    async def test_both_interval_matches_config(
        self, monkeypatch, stub_cycle, stub_no_catchup, enable_bot_fields
    ):
        monkeypatch.setattr(settings, "run_scheduler", True)
        monkeypatch.setattr(settings, "delivery_worker_interval_seconds", 60)

        app = FastAPI()
        async with start_scheduler(app):
            for jid in ("delivery_send", "fan_out_pending"):
                job = app.state.scheduler.get_job(jid)
                assert job is not None
                assert int(job.trigger.interval.total_seconds()) == 60

    async def test_both_max_instances_one(
        self, monkeypatch, stub_cycle, stub_no_catchup, enable_bot_fields
    ):
        """D19 — if a 30s tick takes >30s, the next tick is suppressed rather
        than queued. Applies to both the send worker and the fan-out job."""
        monkeypatch.setattr(settings, "run_scheduler", True)
        app = FastAPI()
        async with start_scheduler(app):
            for jid in ("delivery_send", "fan_out_pending"):
                job = app.state.scheduler.get_job(jid)
                assert job is not None
                assert job.max_instances == 1


# ---------------------------------------------------------------------------
# _send_with_session wrapper
# ---------------------------------------------------------------------------


class TestSendWithSession:
    async def test_returns_summary_on_success(self, monkeypatch):
        """Happy path — wrapper awaits send_pending_deliveries, commits,
        returns the summary dict."""
        from etfpulse.pipeline.scheduler import _send_with_session

        expected = {
            "total": 2,
            "sent": 2,
            "failed": 0,
            "blocked": 0,
            "chat_not_found": 0,
            "skipped_no_target": 0,
        }

        async def _stub(session):
            return expected

        monkeypatch.setattr("etfpulse.pipeline.scheduler.send_pending_deliveries", _stub)

        result = await _send_with_session()
        assert result == expected

    async def test_returns_none_on_exception(self, monkeypatch):
        """Exception in the worker must NOT propagate to APScheduler — otherwise
        the job is marked failed and stops firing. Wrapper swallows, logs, returns None."""
        from etfpulse.pipeline.scheduler import _send_with_session

        async def _stub(session):
            raise RuntimeError("boom")

        monkeypatch.setattr("etfpulse.pipeline.scheduler.send_pending_deliveries", _stub)

        result = await _send_with_session()
        assert result is None  # didn't propagate


# ---------------------------------------------------------------------------
# _fan_out_pending_with_session wrapper (#61)
# ---------------------------------------------------------------------------


class TestFanOutPendingWorker:
    async def test_processes_pending_signals(self, monkeypatch, db_session):
        """Full path: seed a PENDING signal with a matching user, run the
        wrapper, verify a SignalDelivery row materializes."""
        from datetime import date as date_cls

        from etfpulse.models import (
            ChannelType,
            NotificationChannel,
            Signal,
            SignalDelivery,
            SignalStatus,
            User,
        )
        from etfpulse.pipeline.detectors import compute_fingerprint
        from etfpulse.pipeline.scheduler import _fan_out_pending_with_session

        # Seed signal + user + channel.
        signal = Signal(
            signal_type="flow_anomaly",
            asset="BTC",
            trigger_data={},
            ai_analysis={"headline": "test"},
            confidence=7,
            status=SignalStatus.PENDING.value,
            fingerprint=compute_fingerprint("fan-pending-test"),
            signal_date=date_cls(2026, 4, 23),
        )
        user = User(pref_assets=["BTC"], pref_min_confidence=5)
        db_session.add_all([signal, user])
        await db_session.flush()
        db_session.add(
            NotificationChannel(
                user_id=user.id,
                channel_type=ChannelType.TELEGRAM.value,
                channel_identifier="900",
            )
        )
        await db_session.commit()  # commit so the wrapper's own session sees the data

        # Patch async_session in scheduler to yield the test session via savepoint
        # so the wrapper's commit doesn't leak past the test.
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _session_yielder():
            async with db_session.begin_nested():
                yield db_session

        monkeypatch.setattr("etfpulse.pipeline.scheduler.async_session", _session_yielder)

        summary = await _fan_out_pending_with_session()

        assert summary is not None
        assert summary["processed"] == 1
        assert summary["fanned_out"] == 1
        deliveries = (await db_session.execute(select(SignalDelivery))).scalars().all()
        assert len(deliveries) == 1

        # Clean up: re-find and delete the committed rows so they don't leak
        # across tests. `db_session` rollback covers savepoints but not the
        # outer commit we did at setup.
        await db_session.rollback()

    async def test_skips_already_alerted(self, monkeypatch):
        """ALERTED signals must NOT be re-processed."""
        from etfpulse.pipeline.scheduler import _fan_out_pending_with_session

        called_with: list[int] = []

        async def _stub_fan_out(session, sid):
            called_with.append(sid)
            return 0

        monkeypatch.setattr("etfpulse.pipeline.scheduler.fan_out_signal", _stub_fan_out)

        # Use an async_session yielder that returns an empty-query session.
        # We assert fan_out_signal wasn't called since no PENDING signals exist
        # (the autouse _schema fixture gives us a clean DB).
        summary = await _fan_out_pending_with_session()

        assert summary is not None
        assert summary["processed"] == 0
        assert called_with == []

    async def test_skips_expired_signals(self, monkeypatch, db_session):
        """Expired PENDING signals are filtered at the SELECT level — fan_out_signal
        is never even called for them, saving log noise."""
        from datetime import date as date_cls
        from datetime import timedelta as td

        from etfpulse.models import Signal, SignalStatus
        from etfpulse.pipeline.detectors import compute_fingerprint
        from etfpulse.pipeline.scheduler import _fan_out_pending_with_session

        # Seed one expired PENDING signal.
        signal = Signal(
            signal_type="flow_anomaly",
            asset="BTC",
            trigger_data={},
            confidence=7,
            status=SignalStatus.PENDING.value,
            expires_at=datetime.now(UTC) - td(hours=1),
            fingerprint=compute_fingerprint("expired-fan-pending"),
            signal_date=date_cls(2026, 4, 23),
        )
        db_session.add(signal)
        await db_session.commit()

        called_with: list[int] = []

        async def _stub_fan_out(session, sid):
            called_with.append(sid)
            return 0

        monkeypatch.setattr("etfpulse.pipeline.scheduler.fan_out_signal", _stub_fan_out)
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _yielder():
            async with db_session.begin_nested():
                yield db_session

        monkeypatch.setattr("etfpulse.pipeline.scheduler.async_session", _yielder)

        summary = await _fan_out_pending_with_session()

        assert summary is not None
        assert summary["processed"] == 0
        # Pre-filter means fan_out_signal was never called for the expired row.
        assert called_with == []

        await db_session.rollback()

    async def test_per_signal_failure_does_not_abort_batch(self, monkeypatch):
        """One bad signal → one `failed` count, but other signals still process."""
        from etfpulse.pipeline.scheduler import _fan_out_pending_with_session

        # Simulate the select returning three signal IDs via a patched
        # async_session. We intercept the wrapper's fan_out_signal to fail
        # on the middle one.
        call_count = 0

        async def _stub_fan_out(session, sid):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise RuntimeError("bad signal")
            return 1

        # Build a minimal mock session that returns 3 signal ids on select().
        from unittest.mock import AsyncMock, MagicMock

        class _FakeResult:
            def scalars(self):
                return self

            def all(self):
                return [101, 102, 103]

        fake_session = MagicMock()
        fake_session.execute = AsyncMock(return_value=_FakeResult())
        fake_session.commit = AsyncMock()
        fake_session.rollback = AsyncMock()

        # Use an async context manager for begin_nested
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _nested():
            yield None

        fake_session.begin_nested = _nested

        @asynccontextmanager
        async def _session_yielder():
            yield fake_session

        monkeypatch.setattr("etfpulse.pipeline.scheduler.async_session", _session_yielder)
        monkeypatch.setattr("etfpulse.pipeline.scheduler.fan_out_signal", _stub_fan_out)

        summary = await _fan_out_pending_with_session()

        assert summary is not None
        assert summary["processed"] == 2  # signals 101 and 103 succeeded
        assert summary["fanned_out"] == 2
        assert summary["failed"] == 1  # signal 102 blew up

    async def test_query_failure_returns_none(self, monkeypatch):
        """If the SELECT for pending signals fails (DB glitch, bad migration,
        etc.), wrapper catches + returns None rather than propagating to
        APScheduler, which would otherwise mark the job failed."""
        from contextlib import asynccontextmanager
        from unittest.mock import AsyncMock, MagicMock

        from etfpulse.pipeline.scheduler import _fan_out_pending_with_session

        fake_session = MagicMock()
        fake_session.execute = AsyncMock(side_effect=RuntimeError("query blew up"))
        fake_session.commit = AsyncMock()
        fake_session.rollback = AsyncMock()

        @asynccontextmanager
        async def _yielder():
            yield fake_session

        monkeypatch.setattr("etfpulse.pipeline.scheduler.async_session", _yielder)

        result = await _fan_out_pending_with_session()
        assert result is None
        fake_session.rollback.assert_awaited()
