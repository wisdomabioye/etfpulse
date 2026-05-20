"""Tests for `pipeline.boot_recovery.flag_orphan_submitted_on_boot`."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from etfpulse.config import settings
from etfpulse.models import (
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    TimeInForce,
    User,
    Venue,
)
from etfpulse.pipeline.boot_recovery import flag_orphan_submitted_on_boot


async def _seed_user(db_session) -> int:
    u = User(wallet_address="0x" + secrets.token_hex(20))
    db_session.add(u)
    await db_session.flush()
    return u.id


async def _seed_order(
    db_session,
    *,
    user_id: int,
    status: str,
    exchange_order_id: str | None,
    updated_at_offset_seconds: int,
    client_order_id: str,
) -> Order:
    o = Order(
        user_id=user_id,
        client_order_id=client_order_id,
        venue=Venue.SODEX_SPOT.value,
        asset="BTC",
        side=OrderSide.BUY.value,
        order_type=OrderType.LIMIT.value,
        time_in_force=TimeInForce.GTC.value,
        requested_size=Decimal("0.01"),
        requested_price=Decimal("65000"),
        status=status,
        exchange_order_id=exchange_order_id,
    )
    db_session.add(o)
    await db_session.flush()
    # Force-set updated_at after the row is persisted; SQLAlchemy
    # server_default applies on insert otherwise.
    o.updated_at = datetime.now(UTC) - timedelta(seconds=updated_at_offset_seconds)
    await db_session.flush()
    return o


async def test_empty_db_returns_clean(db_session):
    summary = await flag_orphan_submitted_on_boot(db_session)
    assert summary == {"orphan_submitted": 0}


async def test_stale_submitted_with_no_exchange_id_counts(db_session):
    """SUBMITTED with NULL exchange_order_id older than the threshold → flagged."""
    uid = await _seed_user(db_session)
    # threshold = 2 * reconcile_interval (default 60s) = 120s. Push order
    # to 200s old.
    await _seed_order(
        db_session,
        user_id=uid,
        status=OrderStatus.SUBMITTED.value,
        exchange_order_id=None,
        updated_at_offset_seconds=200,
        client_order_id="orphan-1",
    )
    summary = await flag_orphan_submitted_on_boot(db_session)
    assert summary["orphan_submitted"] == 1


async def test_recently_submitted_not_counted(db_session):
    """SUBMITTED but very recent — not flagged (reconcile hasn't had a chance yet)."""
    uid = await _seed_user(db_session)
    await _seed_order(
        db_session,
        user_id=uid,
        status=OrderStatus.SUBMITTED.value,
        exchange_order_id=None,
        updated_at_offset_seconds=5,  # 5s old
        client_order_id="recent-1",
    )
    summary = await flag_orphan_submitted_on_boot(db_session)
    assert summary["orphan_submitted"] == 0


async def test_submitted_with_exchange_id_not_counted(db_session):
    """Order is SUBMITTED but has exchange_order_id — race window
    where the row would land ACKED on the next reconcile. Not an
    orphan."""
    uid = await _seed_user(db_session)
    await _seed_order(
        db_session,
        user_id=uid,
        status=OrderStatus.SUBMITTED.value,
        exchange_order_id="123",
        updated_at_offset_seconds=200,
        client_order_id="haseid-1",
    )
    summary = await flag_orphan_submitted_on_boot(db_session)
    assert summary["orphan_submitted"] == 0


async def test_non_submitted_states_ignored(db_session):
    """Only SUBMITTED is scanned — PENDING and beyond don't count."""
    uid = await _seed_user(db_session)
    await _seed_order(
        db_session,
        user_id=uid,
        status=OrderStatus.PENDING.value,
        exchange_order_id=None,
        updated_at_offset_seconds=200,
        client_order_id="pend-1",
    )
    await _seed_order(
        db_session,
        user_id=uid,
        status=OrderStatus.ACKED.value,
        exchange_order_id=None,
        updated_at_offset_seconds=200,
        client_order_id="acked-1",
    )
    summary = await flag_orphan_submitted_on_boot(db_session)
    assert summary["orphan_submitted"] == 0


async def test_does_not_mutate_state(db_session):
    """Pure log/count — Order rows are not modified."""
    uid = await _seed_user(db_session)
    order = await _seed_order(
        db_session,
        user_id=uid,
        status=OrderStatus.SUBMITTED.value,
        exchange_order_id=None,
        updated_at_offset_seconds=200,
        client_order_id="immut-1",
    )
    pre_updated = order.updated_at
    pre_status = order.status

    await flag_orphan_submitted_on_boot(db_session)
    await db_session.refresh(order)
    assert order.updated_at == pre_updated
    assert order.status == pre_status


# Suppress unused-import warning.
_ = settings
