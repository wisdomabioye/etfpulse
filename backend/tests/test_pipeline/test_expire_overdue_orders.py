"""Tests for `pipeline.reapers.expire_overdue_orders`."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from etfpulse.models import (
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    TimeInForce,
    User,
    Venue,
)
from etfpulse.pipeline.reapers import expire_overdue_orders


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
    nonce_expires_at: datetime | None,
    client_order_id: str,
    nonce: int | None = None,
    filled_size: Decimal | None = None,
) -> Order:
    order = Order(
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
        nonce=nonce,
        nonce_expires_at=nonce_expires_at,
        filled_size=filled_size,
    )
    db_session.add(order)
    await db_session.flush()
    return order


@pytest.mark.parametrize(
    "status",
    [
        OrderStatus.PENDING.value,
        OrderStatus.SUBMITTED.value,
        OrderStatus.ACKED.value,
        OrderStatus.PARTIALLY_FILLED.value,
    ],
)
async def test_overdue_non_terminal_expires(db_session, status):
    uid = await _seed_user(db_session)
    past = datetime.now(UTC) - timedelta(hours=1)
    nonce = int(past.timestamp() * 1000)
    order = await _seed_order(
        db_session,
        user_id=uid,
        status=status,
        nonce=nonce,
        nonce_expires_at=past,
        client_order_id=f"o-{status}",
    )
    summary = await expire_overdue_orders(db_session)
    assert summary["expired"] == 1
    await db_session.refresh(order)
    assert order.status == OrderStatus.EXPIRED.value


@pytest.mark.parametrize(
    "status",
    [
        OrderStatus.FILLED.value,
        OrderStatus.CANCELLED.value,
        OrderStatus.REJECTED.value,
        OrderStatus.EXPIRED.value,
    ],
)
async def test_terminal_states_not_touched(db_session, status):
    uid = await _seed_user(db_session)
    past = datetime.now(UTC) - timedelta(hours=1)
    nonce = int(past.timestamp() * 1000)
    order = await _seed_order(
        db_session,
        user_id=uid,
        status=status,
        nonce=nonce,
        nonce_expires_at=past,
        client_order_id=f"o-{status}",
    )
    summary = await expire_overdue_orders(db_session)
    assert summary["expired"] == 0
    await db_session.refresh(order)
    assert order.status == status  # unchanged


async def test_future_nonce_not_touched(db_session):
    uid = await _seed_user(db_session)
    future = datetime.now(UTC) + timedelta(hours=1)
    nonce = int(datetime.now(UTC).timestamp() * 1000)
    order = await _seed_order(
        db_session,
        user_id=uid,
        status=OrderStatus.ACKED.value,
        nonce=nonce,
        nonce_expires_at=future,
        client_order_id="future-1",
    )
    summary = await expire_overdue_orders(db_session)
    assert summary["expired"] == 0
    await db_session.refresh(order)
    assert order.status == OrderStatus.ACKED.value


async def test_null_nonce_expires_not_touched(db_session):
    """Pre-Stage-09 / paper-trade orders with NULL nonce + NULL
    nonce_expires_at must NOT be expired by this reaper — they're
    in a different lifecycle."""
    uid = await _seed_user(db_session)
    order = await _seed_order(
        db_session,
        user_id=uid,
        status=OrderStatus.PENDING.value,
        nonce=None,
        nonce_expires_at=None,
        client_order_id="nullexp-1",
    )
    summary = await expire_overdue_orders(db_session)
    assert summary["expired"] == 0
    await db_session.refresh(order)
    assert order.status == OrderStatus.PENDING.value


async def test_partial_fill_preserved_on_expiry(db_session):
    uid = await _seed_user(db_session)
    past = datetime.now(UTC) - timedelta(hours=1)
    nonce = int(past.timestamp() * 1000)
    order = await _seed_order(
        db_session,
        user_id=uid,
        status=OrderStatus.PARTIALLY_FILLED.value,
        nonce=nonce,
        nonce_expires_at=past,
        client_order_id="partial-1",
        filled_size=Decimal("0.005"),
    )
    summary = await expire_overdue_orders(db_session)
    assert summary["expired"] == 1
    await db_session.refresh(order)
    # Filled portion preserved — only status moves to EXPIRED.
    assert order.status == OrderStatus.EXPIRED.value
    assert order.filled_size == Decimal("0.005")


async def test_idempotent(db_session):
    uid = await _seed_user(db_session)
    past = datetime.now(UTC) - timedelta(hours=1)
    nonce = int(past.timestamp() * 1000)
    await _seed_order(
        db_session,
        user_id=uid,
        status=OrderStatus.ACKED.value,
        nonce=nonce,
        nonce_expires_at=past,
        client_order_id="idemp-1",
    )
    # First run expires 1.
    first = await expire_overdue_orders(db_session)
    assert first["expired"] == 1
    # Second run is a no-op (already EXPIRED, falls out of predicate).
    second = await expire_overdue_orders(db_session)
    assert second["expired"] == 0
