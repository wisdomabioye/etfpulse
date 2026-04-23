"""Pin the partial UNIQUE indexes on `signal_deliveries`.

These indexes are load-bearing for Stage 05's fan_out_signal idempotency —
a second fan-out pass must NOT insert duplicate delivery rows for the same
(signal, recipient) pair. If a future migration accidentally drops these
indexes, fan_out_signal would silently fan signals out multiple times per
user; this test fails loudly instead.

The indexes themselves are created in the initial Alembic migration
(`ux_delivery_user_signal` and `ux_delivery_group_signal`) — this file just
verifies the runtime contract they provide.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from etfpulse.models import (
    ChannelType,
    DeliveryStatus,
    NotificationChannel,
    Signal,
    SignalDelivery,
    TelegramGroup,
    User,
)
from etfpulse.pipeline.detectors import compute_fingerprint


async def _seed_signal(db_session) -> Signal:
    signal = Signal(
        signal_type="flow_anomaly",
        asset="BTC",
        trigger_data={},
        fingerprint=compute_fingerprint("seed", "signal"),
        signal_date=date(2026, 4, 23),
    )
    db_session.add(signal)
    await db_session.flush()
    return signal


async def _seed_user_with_channel(db_session) -> tuple[User, NotificationChannel]:
    user = User()
    db_session.add(user)
    await db_session.flush()

    channel = NotificationChannel(
        user_id=user.id,
        channel_type=ChannelType.TELEGRAM.value,
        channel_identifier="123456",
        username="alice",
    )
    db_session.add(channel)
    await db_session.flush()
    return user, channel


async def test_duplicate_user_delivery_raises_integrity_error(db_session):
    """Two deliveries of the same signal to the same (user, channel) must
    violate `ux_delivery_user_signal`. fan_out_signal will rely on this via
    `ON CONFLICT DO NOTHING` to be idempotent."""
    signal = await _seed_signal(db_session)
    user, channel = await _seed_user_with_channel(db_session)

    db_session.add(
        SignalDelivery(
            signal_id=signal.id,
            user_id=user.id,
            channel_id=channel.id,
            status=DeliveryStatus.PENDING.value,
        )
    )
    await db_session.flush()

    # Second row with same (signal_id, user_id, channel_id) must fail.
    db_session.add(
        SignalDelivery(
            signal_id=signal.id,
            user_id=user.id,
            channel_id=channel.id,
            status=DeliveryStatus.PENDING.value,
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_duplicate_group_delivery_raises_integrity_error(db_session):
    """Same invariant for group deliveries via `ux_delivery_group_signal`."""
    signal = await _seed_signal(db_session)

    group = TelegramGroup(chat_id=-100999, title="Test Group")
    db_session.add(group)
    await db_session.flush()

    db_session.add(
        SignalDelivery(
            signal_id=signal.id,
            group_id=group.id,
            status=DeliveryStatus.PENDING.value,
        )
    )
    await db_session.flush()

    db_session.add(
        SignalDelivery(
            signal_id=signal.id,
            group_id=group.id,
            status=DeliveryStatus.PENDING.value,
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_same_signal_to_different_users_allowed(db_session):
    """Fan-out to multiple users is the main path — must NOT trip the index."""
    signal = await _seed_signal(db_session)

    # Two distinct users, each with their own channel.
    user1, channel1 = await _seed_user_with_channel(db_session)
    user2 = User()
    db_session.add(user2)
    await db_session.flush()
    channel2 = NotificationChannel(
        user_id=user2.id,
        channel_type=ChannelType.TELEGRAM.value,
        channel_identifier="789012",
    )
    db_session.add(channel2)
    await db_session.flush()

    db_session.add(SignalDelivery(signal_id=signal.id, user_id=user1.id, channel_id=channel1.id))
    db_session.add(SignalDelivery(signal_id=signal.id, user_id=user2.id, channel_id=channel2.id))
    await db_session.flush()  # both succeed — no uniqueness collision.


async def test_same_user_different_signals_allowed(db_session):
    """One user getting multiple distinct signals — the normal operating mode."""
    user, channel = await _seed_user_with_channel(db_session)

    for i in range(3):
        signal = Signal(
            signal_type="flow_anomaly",
            asset="BTC",
            trigger_data={},
            fingerprint=compute_fingerprint(f"signal-{i}"),
            signal_date=date(2026, 4, 23),
        )
        db_session.add(signal)
        await db_session.flush()
        db_session.add(SignalDelivery(signal_id=signal.id, user_id=user.id, channel_id=channel.id))
    await db_session.flush()  # all three succeed.


async def test_check_constraint_enforces_user_xor_group(db_session):
    """Sanity — the pre-existing `ck_delivery_target` check constraint still
    holds. A delivery with neither user nor group must fail."""
    signal = await _seed_signal(db_session)

    db_session.add(SignalDelivery(signal_id=signal.id))  # neither target
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()

    # Both set at once is also invalid.
    user, channel = await _seed_user_with_channel(db_session)
    group = TelegramGroup(chat_id=-101000)
    db_session.add(group)
    await db_session.flush()

    db_session.add(
        SignalDelivery(
            signal_id=signal.id,
            user_id=user.id,
            channel_id=channel.id,
            group_id=group.id,  # XOR violation
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_price_at_creation_decimal_round_trip(db_session):
    """Unrelated but adjacent — pin that Decimal on Signal round-trips
    cleanly (used by fan_out to filter expired signals)."""
    signal = Signal(
        signal_type="flow_anomaly",
        asset="BTC",
        trigger_data={},
        price_at_creation=Decimal("42000.50"),
        fingerprint=compute_fingerprint("price-test"),
        signal_date=date(2026, 4, 23),
    )
    db_session.add(signal)
    await db_session.flush()

    assert signal.price_at_creation == Decimal("42000.50")
