"""`next_nonce_for_user` — monotonic millisecond nonce per user.

The PRECONDITION (caller holds FOR UPDATE on the user row) cannot be
asserted by the function itself — Postgres provides no introspection
for "do I hold a row lock". These tests pin the OBSERVABLE behaviour:

  - Returns ≥ now_ms (wall-clock anchor).
  - Returns > all existing Order.nonce values for the user (strict
    monotonic).
  - User-scoped: another user's nonces don't pollute the floor.
  - Within the same ms, `last + 1` wins (test path; production rarely
    hits this branch because wall-clock dominates).

The FOR UPDATE contract is exercised end-to-end at the orchestrator
level (`test_execution_pipeline_*.py` in step 6), not here.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from etfpulse.models import Order, OrderSide, OrderStatus, OrderType, TimeInForce, User, Venue
from etfpulse.pipeline.execution.nonce import next_nonce_for_user


def _make_order(*, user_id: int, nonce: int, client_order_id: str) -> Order:
    """Build a minimal valid Order pinned to a specific nonce."""
    return Order(
        user_id=user_id,
        client_order_id=client_order_id,
        venue=Venue.SODEX_SPOT.value,
        asset="BTC",
        side=OrderSide.BUY.value,
        order_type=OrderType.LIMIT.value,
        time_in_force=TimeInForce.GTC.value,
        requested_size=Decimal("0.01"),
        status=OrderStatus.PENDING.value,
        nonce=nonce,
        nonce_expires_at=datetime.now(UTC) + timedelta(days=1),
    )


async def _seed_user(db_session) -> int:
    u = User()
    db_session.add(u)
    await db_session.flush()
    return u.id


class TestWallClockAnchor:
    async def test_no_prior_orders_returns_now_ms(self, db_session):
        uid = await _seed_user(db_session)
        before = int(time.time() * 1000)
        nonce = await next_nonce_for_user(db_session, uid)
        after = int(time.time() * 1000)
        # Wall-clock anchored: nonce sits between before and after
        # (allowing for the function's own time.time() call).
        assert before <= nonce <= after + 1

    async def test_far_past_nonce_doesnt_drag_now(self, db_session):
        """A user with an old (much earlier than now) max nonce should
        still get the wall-clock value, not `last+1`."""
        uid = await _seed_user(db_session)
        ancient = 1_000_000_000_000  # ~2001 in ms
        db_session.add(_make_order(user_id=uid, nonce=ancient, client_order_id="anc-1"))
        await db_session.flush()

        nonce = await next_nonce_for_user(db_session, uid)
        assert nonce > ancient + 1
        # Wall-clock branch won.
        assert nonce >= int(time.time() * 1000) - 1


class TestMonotonic:
    async def test_strictly_greater_than_last(self, db_session):
        uid = await _seed_user(db_session)
        # Plant a max nonce just BELOW now_ms so the +1 branch is
        # exercised. now_ms-1 means "last fired 1ms ago" — common in
        # bot-rate submission.
        now_ms = int(time.time() * 1000)
        db_session.add(_make_order(user_id=uid, nonce=now_ms - 1, client_order_id="m-1"))
        await db_session.flush()

        nonce = await next_nonce_for_user(db_session, uid)
        assert nonce > now_ms - 1
        assert nonce >= now_ms

    async def test_future_nonce_wins(self, db_session):
        """If the last nonce is somehow in the future (clock skew between
        boxes, a bot pre-loaded a high nonce), the function returns
        `last + 1` — never goes backwards."""
        uid = await _seed_user(db_session)
        future = int(time.time() * 1000) + 60_000  # 60s in the future
        db_session.add(_make_order(user_id=uid, nonce=future, client_order_id="f-1"))
        await db_session.flush()

        nonce = await next_nonce_for_user(db_session, uid)
        assert nonce == future + 1


class TestUserScoping:
    async def test_other_users_nonces_ignored(self, db_session):
        """User A's high nonce must NOT affect User B's next nonce."""
        uid_a = await _seed_user(db_session)
        uid_b = await _seed_user(db_session)
        far_future = int(time.time() * 1000) + 86_400_000  # 24h ahead
        db_session.add(_make_order(user_id=uid_a, nonce=far_future, client_order_id="a-1"))
        await db_session.flush()

        nonce_b = await next_nonce_for_user(db_session, uid_b)
        # User B sees only its own (empty) history → wall-clock anchor.
        assert nonce_b < far_future


class TestSameMsCollisionGuard:
    async def test_two_consecutive_calls_strictly_increasing(self, db_session):
        """Within the same ms, the second call after persisting an Order
        from the first must return > the first.

        This test mirrors the production sequence: prepare_order calls
        next_nonce, INSERTs the Order, commits; next prepare for the
        SAME user reads the now-committed nonce and returns one higher.
        """
        uid = await _seed_user(db_session)
        first = await next_nonce_for_user(db_session, uid)
        db_session.add(_make_order(user_id=uid, nonce=first, client_order_id="s-1"))
        await db_session.flush()

        second = await next_nonce_for_user(db_session, uid)
        assert second > first


@pytest.mark.parametrize("n_orders", [0, 1, 5])
async def test_property_returns_int(db_session, n_orders: int):
    """Type assertion — always returns a Python int suitable for
    EIP-712 numeric serialisation."""
    uid = await _seed_user(db_session)
    base_nonce = int(time.time() * 1000) - 1000
    for i in range(n_orders):
        db_session.add(_make_order(user_id=uid, nonce=base_nonce + i, client_order_id=f"p-{i}"))
    await db_session.flush()

    nonce = await next_nonce_for_user(db_session, uid)
    assert isinstance(nonce, int)
    assert nonce > 0
