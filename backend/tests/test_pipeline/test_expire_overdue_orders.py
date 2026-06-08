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
    is_conditional: bool = False,
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
        is_conditional=is_conditional,
    )
    db_session.add(order)
    await db_session.flush()
    return order


@pytest.mark.parametrize(
    "status",
    [
        OrderStatus.PENDING.value,
        OrderStatus.SUBMITTED.value,
    ],
)
async def test_never_accepted_overdue_expires(db_session, status):
    """PENDING / SUBMITTED orders past the nonce window were never
    venue-accepted — their signed payload can't be submitted anymore →
    EXPIRE."""
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
        OrderStatus.ACKED.value,
        OrderStatus.PARTIALLY_FILLED.value,
    ],
)
@pytest.mark.parametrize("is_conditional", [False, True])
async def test_venue_live_overdue_not_expired(db_session, status, is_conditional):
    """PR P1-fix.REAP-2 — ACKED / PARTIALLY_FILLED orders are LIVE on the
    venue; the nonce is spent. The reaper MUST NOT expire them, even past
    the nonce window, regardless of conditionality — a local EXPIRE would
    create DB/venue drift (the row drops out of reconcile) and break
    GTC-limit / resting-stop semantics. They leave the active set only on
    a real terminal transition (fill / cancel / reject)."""
    uid = await _seed_user(db_session)
    past = datetime.now(UTC) - timedelta(hours=1)
    nonce = int(past.timestamp() * 1000)
    order = await _seed_order(
        db_session,
        user_id=uid,
        status=status,
        nonce=nonce,
        nonce_expires_at=past,
        client_order_id=f"o-{status}-{is_conditional}",
        is_conditional=is_conditional,
    )
    summary = await expire_overdue_orders(db_session)
    assert summary["expired"] == 0
    await db_session.refresh(order)
    assert order.status == status  # untouched


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
    """A never-accepted (PENDING) order whose nonce window is still open
    must NOT be reaped — the signed payload is still submittable."""
    uid = await _seed_user(db_session)
    future = datetime.now(UTC) + timedelta(hours=1)
    nonce = int(datetime.now(UTC).timestamp() * 1000)
    order = await _seed_order(
        db_session,
        user_id=uid,
        status=OrderStatus.PENDING.value,
        nonce=nonce,
        nonce_expires_at=future,
        client_order_id="future-1",
    )
    summary = await expire_overdue_orders(db_session)
    assert summary["expired"] == 0
    await db_session.refresh(order)
    assert order.status == OrderStatus.PENDING.value


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


async def test_idempotent(db_session):
    uid = await _seed_user(db_session)
    past = datetime.now(UTC) - timedelta(hours=1)
    nonce = int(past.timestamp() * 1000)
    await _seed_order(
        db_session,
        user_id=uid,
        status=OrderStatus.PENDING.value,
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


# ---------------------------------------------------------------------------
# PR P1-fix.REAP-2 — venue-live exemption (the ACKED/PARTIALLY_FILLED
# not-reaped case is covered by test_venue_live_overdue_not_expired,
# parametrized over is_conditional). This pins the COMPLEMENT: a
# conditional order that is NOT yet venue-live IS still reaped.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status",
    [OrderStatus.PENDING.value, OrderStatus.SUBMITTED.value],
)
async def test_unsubmitted_conditional_still_expired(db_session, status):
    """A conditional order that is NOT yet venue-live (PENDING/SUBMITTED)
    IS still reapable — its signed payload genuinely can't be submitted
    past the nonce window, so it's a zombie, not a resting stop."""
    uid = await _seed_user(db_session)
    past = datetime.now(UTC) - timedelta(hours=1)
    nonce = int(past.timestamp() * 1000)
    order = await _seed_order(
        db_session,
        user_id=uid,
        status=status,
        nonce=nonce,
        nonce_expires_at=past,
        client_order_id=f"cond-unsub-{status}",
        is_conditional=True,
    )
    summary = await expire_overdue_orders(db_session)
    assert summary["expired"] == 1
    await db_session.refresh(order)
    assert order.status == OrderStatus.EXPIRED.value
