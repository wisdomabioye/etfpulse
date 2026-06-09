"""Unit tests for `pipeline.execution.risk.compute_usage` — the read-only
usage snapshot behind GET /api/execution/limits.

It MUST report the same numbers the gate enforces (it reuses the gate's
`_count_open_orders` + `_sum_notional` + `NOTIONAL_WINDOW`), so these pin:
  - empty user → all zeros (per_symbol None when no asset).
  - open-order count vs notional have DIFFERENT membership: FILLED counts
    toward notional (capital at risk) but NOT the open-order cap (terminal);
    an order older than the 24h window counts toward open-orders (no window
    on that count) but NOT toward notional.
  - terminal (CANCELLED/REJECTED/EXPIRED) counts toward neither.
  - asset scopes per_symbol; daily is asset-independent.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from etfpulse.models import Order, User
from etfpulse.models.order import OrderSide as DbOrderSide
from etfpulse.models.order import OrderStatus, Venue
from etfpulse.models.order import OrderType as DbOrderType
from etfpulse.models.order import TimeInForce as DbTimeInForce
from etfpulse.pipeline.execution.risk import NOTIONAL_WINDOW, compute_usage

pytestmark = pytest.mark.asyncio


async def _make_user(db_session) -> User:
    u = User(wallet_address="0x" + secrets.token_hex(20), sodex_account_id=1)
    db_session.add(u)
    await db_session.flush()
    return u


_COID = iter(range(1, 1_000_000))


async def _order(
    db_session,
    *,
    user_id: int,
    status: str = OrderStatus.ACKED.value,
    size: Decimal = Decimal("0.01"),
    price: Decimal | None = Decimal("65000"),
    asset: str = "BTC",
    venue: str = Venue.SODEX_SPOT.value,
    age: timedelta | None = None,
) -> Order:
    o = Order(
        user_id=user_id,
        client_order_id=f"co-{next(_COID)}",
        venue=venue,
        asset=asset,
        side=DbOrderSide.BUY.value,
        order_type=DbOrderType.LIMIT.value,
        time_in_force=DbTimeInForce.GTC.value,
        requested_size=size,
        requested_price=price,
        status=status,
    )
    if age is not None:
        o.created_at = datetime.now(UTC) - age
    db_session.add(o)
    await db_session.flush()
    return o


async def test_empty_user_is_all_zero(db_session):
    u = await _make_user(db_session)
    usage = await compute_usage(db_session, user_id=u.id, asset="BTC")
    assert usage.open_orders == 0
    assert usage.daily_notional == Decimal("0")
    assert usage.per_symbol_notional == Decimal("0")


async def test_asset_none_skips_per_symbol(db_session):
    u = await _make_user(db_session)
    await _order(db_session, user_id=u.id)
    usage = await compute_usage(db_session, user_id=u.id, asset=None)
    assert usage.per_symbol_notional is None
    assert usage.daily_notional == Decimal("650.00")  # 0.01 * 65000


async def test_sums_open_orders_and_notional(db_session):
    u = await _make_user(db_session)
    await _order(db_session, user_id=u.id, size=Decimal("0.01"), price=Decimal("65000"))  # 650
    await _order(db_session, user_id=u.id, size=Decimal("0.02"), price=Decimal("65000"))  # 1300
    usage = await compute_usage(db_session, user_id=u.id, asset="BTC")
    assert usage.open_orders == 2
    assert usage.daily_notional == Decimal("1950.00")
    assert usage.per_symbol_notional == Decimal("1950.00")


async def test_per_symbol_scopes_to_asset(db_session):
    u = await _make_user(db_session)
    await _order(db_session, user_id=u.id, asset="BTC", size=Decimal("0.01"))  # 650
    await _order(
        db_session, user_id=u.id, asset="ETH", size=Decimal("1"), price=Decimal("3000")
    )  # 3000
    usage = await compute_usage(db_session, user_id=u.id, asset="BTC")
    assert usage.daily_notional == Decimal("3650.00")  # both
    assert usage.per_symbol_notional == Decimal("650.00")  # BTC only
    assert usage.open_orders == 2


async def test_filled_counts_notional_not_open_count(db_session):
    """FILLED is terminal (drops from open-order cap) but capital is still at
    risk, so it stays in the 24h notional."""
    u = await _make_user(db_session)
    await _order(db_session, user_id=u.id, status=OrderStatus.ACKED.value)  # open + notional
    await _order(db_session, user_id=u.id, status=OrderStatus.FILLED.value)  # notional only
    usage = await compute_usage(db_session, user_id=u.id, asset="BTC")
    assert usage.open_orders == 1
    assert usage.daily_notional == Decimal("1300.00")  # both 650


async def test_terminal_excluded_from_both(db_session):
    u = await _make_user(db_session)
    await _order(db_session, user_id=u.id, status=OrderStatus.CANCELLED.value)
    await _order(db_session, user_id=u.id, status=OrderStatus.REJECTED.value)
    await _order(db_session, user_id=u.id, status=OrderStatus.EXPIRED.value)
    usage = await compute_usage(db_session, user_id=u.id, asset="BTC")
    assert usage.open_orders == 0
    assert usage.daily_notional == Decimal("0")


async def test_old_order_counts_open_but_not_notional(db_session):
    """No window on the open-order count; a 25h-old ACKED order still counts
    toward the open cap but has aged out of the 24h notional window."""
    u = await _make_user(db_session)
    await _order(db_session, user_id=u.id, age=NOTIONAL_WINDOW + timedelta(hours=1))
    usage = await compute_usage(db_session, user_id=u.id, asset="BTC")
    assert usage.open_orders == 1
    assert usage.daily_notional == Decimal("0")
    assert usage.per_symbol_notional == Decimal("0")
