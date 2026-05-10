"""Reapers — issues #30 (signal expiry) + #36 (stuck deliveries)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select

from etfpulse.config import settings
from etfpulse.models import (
    ChannelType,
    DeliveryStatus,
    NotificationChannel,
    Signal,
    SignalDelivery,
    SignalStatus,
    User,
)
from etfpulse.pipeline.detectors.base import compute_fingerprint
from etfpulse.pipeline.reapers import (
    DELIVERY_REAPER_ERROR,
    expire_overdue_signals,
    fail_stuck_deliveries,
)

# ---------------------------------------------------------------------------
# Factories — minimal, local to this file. Mirror the patterns in
# test_pipeline/test_delivery.py so reads are familiar.
# ---------------------------------------------------------------------------


async def _make_signal(
    db_session,
    *,
    fp_seed: str,
    status: str = SignalStatus.PENDING.value,
    expires_at: datetime | None = None,
) -> Signal:
    signal = Signal(
        signal_type="flow_anomaly",
        asset="BTC",
        trigger_data={},
        status=status,
        expires_at=expires_at,
        fingerprint=compute_fingerprint("BTC", "reaper-test", fp_seed),
        signal_date=date(2026, 4, 23),
    )
    db_session.add(signal)
    await db_session.flush()
    return signal


async def _make_delivery(
    db_session,
    signal_id: int,
    *,
    status: str = DeliveryStatus.PENDING.value,
    created_at: datetime | None = None,
    error_message: str | None = None,
) -> SignalDelivery:
    user = User(pref_assets=["BTC"], pref_min_confidence=5)
    db_session.add(user)
    await db_session.flush()
    channel = NotificationChannel(
        user_id=user.id,
        channel_type=ChannelType.TELEGRAM.value,
        channel_identifier=f"chat-{user.id}",
    )
    db_session.add(channel)
    await db_session.flush()

    delivery = SignalDelivery(
        signal_id=signal_id,
        user_id=user.id,
        channel_id=channel.id,
        status=status,
        error_message=error_message,
    )
    db_session.add(delivery)
    await db_session.flush()
    # `created_at` is server_default; override AFTER insert for the rows we
    # want to "look old". Same pattern as test_delivery_uniqueness.py.
    if created_at is not None:
        delivery.created_at = created_at
        await db_session.flush()
    return delivery


# ---------------------------------------------------------------------------
# expire_overdue_signals — issue #30
# ---------------------------------------------------------------------------


class TestExpireOverdueSignals:
    async def test_flips_pending_with_past_expires_at(self, db_session):
        """Past expires_at + status=pending → status=expired."""
        s = await _make_signal(
            db_session,
            fp_seed="overdue-pending",
            status=SignalStatus.PENDING.value,
            expires_at=datetime.now(UTC) - timedelta(hours=1),
        )

        summary = await expire_overdue_signals(db_session)
        assert summary == {"expired": 1}

        await db_session.refresh(s)
        assert s.status == SignalStatus.EXPIRED.value

    async def test_flips_alerted_with_past_expires_at(self, db_session):
        """ALERTED is reachable too — fan-out happened, then expiry hit."""
        s = await _make_signal(
            db_session,
            fp_seed="overdue-alerted",
            status=SignalStatus.ALERTED.value,
            expires_at=datetime.now(UTC) - timedelta(minutes=30),
        )

        summary = await expire_overdue_signals(db_session)
        assert summary == {"expired": 1}

        await db_session.refresh(s)
        assert s.status == SignalStatus.EXPIRED.value

    async def test_skips_future_expires_at(self, db_session):
        """Future expires_at → no change. Idempotent on healthy rows."""
        s = await _make_signal(
            db_session,
            fp_seed="future",
            status=SignalStatus.PENDING.value,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

        summary = await expire_overdue_signals(db_session)
        assert summary == {"expired": 0}

        await db_session.refresh(s)
        assert s.status == SignalStatus.PENDING.value

    async def test_skips_null_expires_at(self, db_session):
        """AI-failure signals (NULL expires_at) are intentionally NOT touched —
        their horizon is indeterminate. Documented in module docstring."""
        s = await _make_signal(
            db_session,
            fp_seed="ai-failed",
            status=SignalStatus.PENDING.value,
            expires_at=None,
        )

        summary = await expire_overdue_signals(db_session)
        assert summary == {"expired": 0}

        await db_session.refresh(s)
        assert s.status == SignalStatus.PENDING.value

    async def test_idempotent_on_already_expired(self, db_session):
        """Status already EXPIRED → no rowcount, no churn."""
        await _make_signal(
            db_session,
            fp_seed="already-expired",
            status=SignalStatus.EXPIRED.value,
            expires_at=datetime.now(UTC) - timedelta(hours=1),
        )

        summary = await expire_overdue_signals(db_session)
        assert summary == {"expired": 0}

    async def test_bulk_update_handles_many_rows(self, db_session):
        """Reaper is bulk — exercise it with multiple rows."""
        for i in range(5):
            await _make_signal(
                db_session,
                fp_seed=f"bulk-{i}",
                status=SignalStatus.PENDING.value,
                expires_at=datetime.now(UTC) - timedelta(hours=1),
            )
        # One healthy row to ensure the WHERE clause is selective.
        await _make_signal(
            db_session,
            fp_seed="healthy",
            status=SignalStatus.PENDING.value,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

        summary = await expire_overdue_signals(db_session)
        assert summary == {"expired": 5}

        # Verify the healthy row is untouched.
        result = await db_session.execute(
            select(Signal.status).where(
                Signal.fingerprint == compute_fingerprint("BTC", "reaper-test", "healthy")
            )
        )
        assert result.scalar_one() == SignalStatus.PENDING.value


# ---------------------------------------------------------------------------
# fail_stuck_deliveries — issue #36
# ---------------------------------------------------------------------------


@pytest.fixture
def short_delivery_max_age(monkeypatch):
    """Lower the threshold so tests don't have to fabricate ancient timestamps.

    600s default is fine for prod but cumbersome to assert against. 60s is
    the field's minimum (`Field(ge=60)`).
    """
    monkeypatch.setattr(settings, "delivery_pending_max_age_seconds", 60)


class TestFailStuckDeliveries:
    async def test_flips_old_pending_to_failed(self, db_session, short_delivery_max_age):
        s = await _make_signal(db_session, fp_seed="stuck-1")
        d = await _make_delivery(
            db_session,
            s.id,
            status=DeliveryStatus.PENDING.value,
            created_at=datetime.now(UTC) - timedelta(minutes=5),
        )

        summary = await fail_stuck_deliveries(db_session)
        assert summary == {"stuck_failed": 1}

        await db_session.refresh(d)
        assert d.status == DeliveryStatus.FAILED.value
        assert d.error_message == DELIVERY_REAPER_ERROR

    async def test_skips_recent_pending(self, db_session, short_delivery_max_age):
        """Created within threshold → not stuck → not touched."""
        s = await _make_signal(db_session, fp_seed="recent")
        d = await _make_delivery(
            db_session,
            s.id,
            status=DeliveryStatus.PENDING.value,
            # Now-ish; well under the 60s threshold.
            created_at=datetime.now(UTC) - timedelta(seconds=5),
        )

        summary = await fail_stuck_deliveries(db_session)
        assert summary == {"stuck_failed": 0}

        await db_session.refresh(d)
        assert d.status == DeliveryStatus.PENDING.value

    async def test_skips_already_delivered(self, db_session, short_delivery_max_age):
        """DELIVERED rows are terminal — reaper must NOT touch them."""
        s = await _make_signal(db_session, fp_seed="delivered")
        d = await _make_delivery(
            db_session,
            s.id,
            status=DeliveryStatus.DELIVERED.value,
            created_at=datetime.now(UTC) - timedelta(hours=1),
        )

        summary = await fail_stuck_deliveries(db_session)
        assert summary == {"stuck_failed": 0}

        await db_session.refresh(d)
        assert d.status == DeliveryStatus.DELIVERED.value

    async def test_skips_already_failed(self, db_session, short_delivery_max_age):
        """FAILED rows have audit-trail value (real error_message). Don't
        overwrite their error_message with the reaper sentinel."""
        s = await _make_signal(db_session, fp_seed="already-failed")
        d = await _make_delivery(
            db_session,
            s.id,
            status=DeliveryStatus.FAILED.value,
            created_at=datetime.now(UTC) - timedelta(hours=1),
            error_message="real telegram error",
        )

        summary = await fail_stuck_deliveries(db_session)
        assert summary == {"stuck_failed": 0}

        await db_session.refresh(d)
        assert d.status == DeliveryStatus.FAILED.value
        assert d.error_message == "real telegram error"

    async def test_bulk_handles_mix_of_states(self, db_session, short_delivery_max_age):
        s = await _make_signal(db_session, fp_seed="bulk-mix")
        old = datetime.now(UTC) - timedelta(minutes=10)
        recent = datetime.now(UTC) - timedelta(seconds=5)

        # 3 stuck PENDING — should flip
        for _ in range(3):
            await _make_delivery(
                db_session,
                s.id,
                status=DeliveryStatus.PENDING.value,
                created_at=old,
            )
        # 1 recent PENDING — shouldn't flip
        await _make_delivery(
            db_session,
            s.id,
            status=DeliveryStatus.PENDING.value,
            created_at=recent,
        )
        # 1 old DELIVERED, 1 old FAILED — shouldn't flip
        await _make_delivery(
            db_session,
            s.id,
            status=DeliveryStatus.DELIVERED.value,
            created_at=old,
        )
        await _make_delivery(
            db_session,
            s.id,
            status=DeliveryStatus.FAILED.value,
            created_at=old,
            error_message="other error",
        )

        summary = await fail_stuck_deliveries(db_session)
        assert summary == {"stuck_failed": 3}
