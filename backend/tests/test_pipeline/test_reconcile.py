"""Tests for `pipeline.reconcile.reconcile_open_orders`.

Mock spot/perps clients pin the venue's responses; the test verifies
the orchestrator correctly folds fills, opens Positions, and logs
drift. Real HTTP never runs.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from etfpulse.adapters.sodex.perps_client import SodexPerpsClient
from etfpulse.adapters.sodex.responses import (
    AccountBalances,
    BalanceEntry,
    OpenOrdersResponse,
    OpenPositionsResponse,
)
from etfpulse.adapters.sodex.spot_client import SodexSpotClient
from etfpulse.models import (
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    PositionStatus,
    TimeInForce,
    User,
    Venue,
)
from etfpulse.pipeline.reconcile import reconcile_open_orders

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


async def _seed_user(db_session) -> User:
    u = User(
        wallet_address="0x" + secrets.token_hex(20),
        sodex_account_id=57436,
        sodex_spot_api_key_name="default",
        sodex_perps_api_key_name="default",
    )
    db_session.add(u)
    await db_session.flush()
    return u


async def _seed_order(
    db_session,
    *,
    user_id: int,
    client_order_id: str,
    venue: str = Venue.SODEX_SPOT.value,
    status: str = OrderStatus.ACKED.value,
    side: str = OrderSide.BUY.value,
    requested_size: Decimal = Decimal("0.01"),
    filled_size: Decimal | None = None,
    age_seconds: int = 120,
) -> Order:
    o = Order(
        user_id=user_id,
        client_order_id=client_order_id,
        venue=venue,
        asset="BTC",
        side=side,
        order_type=OrderType.LIMIT.value,
        time_in_force=TimeInForce.GTC.value,
        requested_size=requested_size,
        requested_price=Decimal("65000"),
        filled_size=filled_size,
        status=status,
    )
    db_session.add(o)
    await db_session.flush()
    # Backdate updated_at past the min_age cutoff so reconcile picks it up.
    # PR D.3.1 — reconcile now filters min_age on created_at; backdate
    # both for safety so tests don't depend on the filter choice.
    aged_at = datetime.now(UTC) - timedelta(seconds=age_seconds)
    o.updated_at = aged_at
    o.created_at = aged_at
    await db_session.flush()
    return o


def _make_spot_client(*, open_orders: list[dict], balances: list[BalanceEntry]) -> AsyncMock:
    """Build a mocked SodexSpotClient. `model_construct` bypasses the
    required-field validators on the DTOs — fields we don't read in
    reconcile (blockHeight, blockTime) stay unset, which is fine for
    the orchestrator's perspective."""
    client = AsyncMock(spec=SodexSpotClient)
    client.get_open_orders = AsyncMock(
        return_value=OpenOrdersResponse.model_construct(orders=open_orders)
    )
    client.get_balances = AsyncMock(return_value=AccountBalances.model_construct(balances=balances))
    return client


def _make_perps_client(*, open_orders: list[dict], positions: list[dict]) -> AsyncMock:
    client = AsyncMock(spec=SodexPerpsClient)
    client.get_open_orders = AsyncMock(
        return_value=OpenOrdersResponse.model_construct(orders=open_orders)
    )
    client.get_positions = AsyncMock(
        return_value=OpenPositionsResponse.model_construct(positions=positions)
    )
    return client


# ---------------------------------------------------------------------------
# Empty sweep
# ---------------------------------------------------------------------------


class TestEmptySweep:
    async def test_no_active_orders_returns_clean(self, db_session):
        spot = _make_spot_client(open_orders=[], balances=[])
        perps = _make_perps_client(open_orders=[], positions=[])
        summary = await reconcile_open_orders(db_session, spot_client=spot, perps_client=perps)
        assert summary["users_processed"] == 0
        assert summary["orders_filled"] == 0
        # No HTTP calls (no users to probe).
        spot.get_open_orders.assert_not_called()


# ---------------------------------------------------------------------------
# Fill detection
# ---------------------------------------------------------------------------


class TestFillDetection:
    async def test_full_fill_transitions_to_filled_and_opens_position(self, db_session):
        user = await _seed_user(db_session)
        order = await _seed_order(
            db_session,
            user_id=user.id,
            client_order_id="o-fill-1",
            requested_size=Decimal("0.01"),
        )

        spot = _make_spot_client(
            open_orders=[
                {
                    "clOrdID": "o-fill-1",
                    "filledQuantity": "0.01",
                    "filledFunds": "650",
                    "avgPrice": "65000",
                }
            ],
            balances=[BalanceEntry.model_validate({"i": 1, "a": "vBTC", "t": "0.01", "l": "0"})],
        )
        perps = _make_perps_client(open_orders=[], positions=[])

        summary = await reconcile_open_orders(db_session, spot_client=spot, perps_client=perps)
        assert summary["orders_filled"] == 1
        assert summary["users_processed"] == 1

        await db_session.refresh(order)
        assert order.status == OrderStatus.FILLED.value
        assert order.filled_size == Decimal("0.01")

        # Position opened.
        position = (
            await db_session.execute(select(Position).where(Position.user_id == user.id))
        ).scalar_one()
        assert position.size == Decimal("0.01")
        assert position.entry_price == Decimal("65000")

    async def test_partial_fill_transitions_to_partially_filled(self, db_session):
        user = await _seed_user(db_session)
        order = await _seed_order(
            db_session,
            user_id=user.id,
            client_order_id="o-pf-1",
            requested_size=Decimal("0.02"),
        )

        spot = _make_spot_client(
            open_orders=[
                {
                    "clOrdID": "o-pf-1",
                    "filledQuantity": "0.01",
                    "filledFunds": "650",
                    "avgPrice": "65000",
                }
            ],
            balances=[BalanceEntry.model_validate({"i": 1, "a": "vBTC", "t": "0.01", "l": "0"})],
        )
        perps = _make_perps_client(open_orders=[], positions=[])

        summary = await reconcile_open_orders(db_session, spot_client=spot, perps_client=perps)
        assert summary["orders_partially_filled"] == 1
        await db_session.refresh(order)
        assert order.status == OrderStatus.PARTIALLY_FILLED.value
        assert order.filled_size == Decimal("0.01")

    async def test_delta_fill_only_applies_new_amount(self, db_session):
        """Second sweep: venue went from 0.01 → 0.02. Apply only the
        0.01 delta, not the cumulative 0.02."""
        user = await _seed_user(db_session)
        order = await _seed_order(
            db_session,
            user_id=user.id,
            client_order_id="o-delta-1",
            requested_size=Decimal("0.02"),
            filled_size=Decimal("0.01"),
            status=OrderStatus.PARTIALLY_FILLED.value,
        )

        spot = _make_spot_client(
            open_orders=[
                {
                    "clOrdID": "o-delta-1",
                    "filledQuantity": "0.02",  # cumulative
                    "filledFunds": "1300",
                    "avgPrice": "65000",
                }
            ],
            balances=[BalanceEntry.model_validate({"i": 1, "a": "vBTC", "t": "0.02", "l": "0"})],
        )
        perps = _make_perps_client(open_orders=[], positions=[])
        summary = await reconcile_open_orders(db_session, spot_client=spot, perps_client=perps)
        assert summary["orders_filled"] == 1
        await db_session.refresh(order)
        assert order.filled_size == Decimal("0.02")

        position = (
            await db_session.execute(select(Position).where(Position.user_id == user.id))
        ).scalar_one()
        # Position size reflects only the new delta (0.01) — not 0.02.
        # The first 0.01 wasn't applied via reconcile in this test, so
        # Position should have just the delta.
        assert position.size == Decimal("0.01")

    async def test_no_fill_progress_no_op(self, db_session):
        """Venue's filledQuantity == our filled_size → no change."""
        user = await _seed_user(db_session)
        order = await _seed_order(
            db_session,
            user_id=user.id,
            client_order_id="o-nop-1",
            filled_size=Decimal("0.005"),
            status=OrderStatus.PARTIALLY_FILLED.value,
        )

        spot = _make_spot_client(
            open_orders=[
                {
                    "clOrdID": "o-nop-1",
                    "filledQuantity": "0.005",  # same as ours
                    "filledFunds": "325",
                    "avgPrice": "65000",
                }
            ],
            balances=[],
        )
        perps = _make_perps_client(open_orders=[], positions=[])

        summary = await reconcile_open_orders(db_session, spot_client=spot, perps_client=perps)
        assert summary["orders_filled"] == 0
        assert summary["orders_partially_filled"] == 0
        await db_session.refresh(order)
        assert order.status == OrderStatus.PARTIALLY_FILLED.value


# ---------------------------------------------------------------------------
# Drift / orphan
# ---------------------------------------------------------------------------


class TestDriftHandling:
    async def test_order_missing_past_orphan_grace_logged(self, db_session, monkeypatch):
        """Order locally non-terminal, NOT in venue /open list, AND past
        the orphan grace → logged as drift."""
        # Tighten grace to small for the test.
        from etfpulse.config import settings

        monkeypatch.setattr(settings, "order_reconcile_orphan_grace_seconds", 60)

        user = await _seed_user(db_session)
        # Backdate the order well past the grace.
        await _seed_order(
            db_session,
            user_id=user.id,
            client_order_id="orphan-1",
            age_seconds=600,
        )

        spot = _make_spot_client(
            open_orders=[],  # venue says no open orders
            balances=[],
        )
        perps = _make_perps_client(open_orders=[], positions=[])

        summary = await reconcile_open_orders(db_session, spot_client=spot, perps_client=perps)
        assert summary["orders_drift_unmatched"] == 1

    async def test_order_within_orphan_grace_not_logged(self, db_session, monkeypatch):
        """Same scenario but young — within grace → not flagged."""
        from etfpulse.config import settings

        # Wide grace so even our 120s-old order is "fresh".
        monkeypatch.setattr(settings, "order_reconcile_orphan_grace_seconds", 600)

        user = await _seed_user(db_session)
        await _seed_order(
            db_session,
            user_id=user.id,
            client_order_id="young-1",
            age_seconds=120,
        )

        spot = _make_spot_client(open_orders=[], balances=[])
        perps = _make_perps_client(open_orders=[], positions=[])
        summary = await reconcile_open_orders(db_session, spot_client=spot, perps_client=perps)
        assert summary["orders_drift_unmatched"] == 0


# ---------------------------------------------------------------------------
# Per-user isolation
# ---------------------------------------------------------------------------


class TestPerUserIsolation:
    async def test_one_user_http_failure_doesnt_kill_sweep(self, db_session):
        """If one user's GET /open raises, sweep continues for others."""
        u1 = await _seed_user(db_session)
        u2 = await _seed_user(db_session)
        await _seed_order(db_session, user_id=u1.id, client_order_id="u1-1")
        await _seed_order(db_session, user_id=u2.id, client_order_id="u2-1")

        # Make spot client raise on FIRST call only (u1), succeed on second.
        spot = AsyncMock(spec=SodexSpotClient)
        spot.get_open_orders = AsyncMock(
            side_effect=[
                RuntimeError("spot http down"),
                OpenOrdersResponse(orders=[], blockHeight=1, blockTime=1),
            ]
        )
        spot.get_balances = AsyncMock(return_value=AccountBalances.model_construct(balances=[]))
        perps = _make_perps_client(open_orders=[], positions=[])

        summary = await reconcile_open_orders(db_session, spot_client=spot, perps_client=perps)
        # One user errored, one processed.
        assert summary["user_errors"] == 1
        assert summary["users_processed"] == 1


# ---------------------------------------------------------------------------
# Paper isolation
# ---------------------------------------------------------------------------


class TestPaperIsolation:
    async def test_paper_orders_not_probed(self, db_session):
        """Paper orders MUST NOT trigger venue HTTP — the venue has no
        record of them."""
        u = User(
            wallet_address="0x" + secrets.token_hex(20),
            sodex_account_id=57436,
            sodex_spot_api_key_name="default",
            paper_trade=True,
        )
        db_session.add(u)
        await db_session.flush()
        # Paper order — should be EXCLUDED from reconcile.
        o = Order(
            user_id=u.id,
            client_order_id="paper-1",
            venue=Venue.SODEX_SPOT.value,
            asset="BTC",
            side=OrderSide.BUY.value,
            order_type=OrderType.LIMIT.value,
            time_in_force=TimeInForce.GTC.value,
            requested_size=Decimal("0.01"),
            requested_price=Decimal("65000"),
            status=OrderStatus.ACKED.value,
            paper_trade=True,
        )
        db_session.add(o)
        await db_session.flush()
        # PR D.3.1: backdate both timestamps (reconcile filters on created_at).
        aged = datetime.now(UTC) - timedelta(seconds=120)
        o.updated_at = aged
        o.created_at = aged
        await db_session.flush()

        spot = _make_spot_client(open_orders=[], balances=[])
        perps = _make_perps_client(open_orders=[], positions=[])

        summary = await reconcile_open_orders(db_session, spot_client=spot, perps_client=perps)
        # User with only paper orders → never enters the sweep.
        assert summary["users_processed"] == 0
        spot.get_open_orders.assert_not_called()


# Suppress unused-import warning.
_ = PositionStatus
_ = pytest
