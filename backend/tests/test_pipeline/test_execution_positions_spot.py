"""Tests for `pipeline.execution.positions_spot`.

- apply_fill BUY: open, extend (weighted avg).
- apply_fill SELL: reduce, close, accumulate realised PnL, orphan log.
- reconcile: external close, drift detection, orphan log, paper isolation.
"""

from __future__ import annotations

import secrets
from decimal import Decimal

import pytest

from etfpulse.models import (
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    PositionSide,
    PositionStatus,
    TimeInForce,
    User,
    Venue,
)
from etfpulse.pipeline.execution.positions_spot import (
    spot_apply_fill,
    spot_reconcile_positions,
)


def _wallet() -> str:
    return "0x" + secrets.token_hex(20)


async def _seed_user(db_session, *, paper_trade: bool = False) -> int:
    u = User(
        wallet_address=_wallet(),
        sodex_account_id=57436,
        sodex_spot_api_key_name="default",
        paper_trade=paper_trade,
    )
    db_session.add(u)
    await db_session.flush()
    return u.id


async def _seed_order(
    db_session,
    *,
    user_id: int,
    side: str = OrderSide.BUY.value,
    asset: str = "BTC",
    client_order_id: str | None = None,
    paper_trade: bool = False,
    status: str = OrderStatus.FILLED.value,
) -> Order:
    co_id = client_order_id or f"o-{secrets.token_hex(4)}"
    o = Order(
        user_id=user_id,
        client_order_id=co_id,
        venue=Venue.SODEX_SPOT.value,
        asset=asset,
        side=side,
        order_type=OrderType.LIMIT.value,
        time_in_force=TimeInForce.GTC.value,
        requested_size=Decimal("0.01"),
        requested_price=Decimal("65000"),
        status=status,
        paper_trade=paper_trade,
    )
    db_session.add(o)
    await db_session.flush()
    return o


# ---------------------------------------------------------------------------
# BUY: open + extend
# ---------------------------------------------------------------------------


class TestSpotBuyOpen:
    async def test_first_buy_opens_position(self, db_session):
        uid = await _seed_user(db_session)
        order = await _seed_order(db_session, user_id=uid)

        position = await spot_apply_fill(
            db_session,
            order=order,
            fill_price=Decimal("65000"),
            fill_size=Decimal("0.01"),
        )

        assert position is not None
        assert position.user_id == uid
        assert position.asset == "BTC"
        assert position.venue == Venue.SODEX_SPOT.value
        assert position.side == PositionSide.LONG.value
        assert position.size == Decimal("0.01")
        assert position.entry_price == Decimal("65000")
        assert position.status == PositionStatus.OPEN.value
        assert position.order_id == order.id
        assert position.paper_trade is False

    async def test_paper_order_creates_paper_position(self, db_session):
        uid = await _seed_user(db_session, paper_trade=True)
        order = await _seed_order(db_session, user_id=uid, paper_trade=True)

        position = await spot_apply_fill(
            db_session,
            order=order,
            fill_price=Decimal("65000"),
            fill_size=Decimal("0.01"),
        )
        assert position is not None
        assert position.paper_trade is True


class TestSpotBuyExtend:
    async def test_second_buy_aggregates_avg_entry(self, db_session):
        """0.01 @ 60000 + 0.01 @ 70000 → 0.02 @ 65000."""
        uid = await _seed_user(db_session)
        order1 = await _seed_order(db_session, user_id=uid, client_order_id="b-1")
        await spot_apply_fill(
            db_session,
            order=order1,
            fill_price=Decimal("60000"),
            fill_size=Decimal("0.01"),
        )

        order2 = await _seed_order(db_session, user_id=uid, client_order_id="b-2")
        position = await spot_apply_fill(
            db_session,
            order=order2,
            fill_price=Decimal("70000"),
            fill_size=Decimal("0.01"),
        )
        assert position is not None
        assert position.size == Decimal("0.02")
        # Weighted avg: (60000*0.01 + 70000*0.01) / 0.02 = 65000.
        assert position.entry_price == Decimal("65000.00000000")

    async def test_different_asset_creates_separate_position(self, db_session):
        uid = await _seed_user(db_session)
        order_btc = await _seed_order(db_session, user_id=uid, asset="BTC", client_order_id="b-btc")
        order_eth = await _seed_order(db_session, user_id=uid, asset="ETH", client_order_id="b-eth")

        pos_btc = await spot_apply_fill(
            db_session,
            order=order_btc,
            fill_price=Decimal("65000"),
            fill_size=Decimal("0.01"),
        )
        pos_eth = await spot_apply_fill(
            db_session,
            order=order_eth,
            fill_price=Decimal("3000"),
            fill_size=Decimal("0.1"),
        )

        assert pos_btc.id != pos_eth.id
        assert pos_btc.asset == "BTC"
        assert pos_eth.asset == "ETH"


# ---------------------------------------------------------------------------
# SELL: reduce + close
# ---------------------------------------------------------------------------


class TestSpotSell:
    async def test_partial_sell_reduces_and_accumulates_pnl(self, db_session):
        uid = await _seed_user(db_session)
        buy = await _seed_order(db_session, user_id=uid, client_order_id="b-1")
        await spot_apply_fill(
            db_session,
            order=buy,
            fill_price=Decimal("60000"),
            fill_size=Decimal("0.02"),
        )

        sell = await _seed_order(
            db_session,
            user_id=uid,
            side=OrderSide.SELL.value,
            client_order_id="s-1",
        )
        position = await spot_apply_fill(
            db_session,
            order=sell,
            fill_price=Decimal("65000"),
            fill_size=Decimal("0.01"),
        )

        assert position is not None
        assert position.status == PositionStatus.OPEN.value
        assert position.size == Decimal("0.01")
        # PnL: (65000 - 60000) * 0.01 = 50.
        assert position.realized_pnl == Decimal("50.00")

    async def test_full_sell_closes(self, db_session):
        uid = await _seed_user(db_session)
        buy = await _seed_order(db_session, user_id=uid, client_order_id="b-1")
        await spot_apply_fill(
            db_session,
            order=buy,
            fill_price=Decimal("60000"),
            fill_size=Decimal("0.01"),
        )

        sell = await _seed_order(
            db_session,
            user_id=uid,
            side=OrderSide.SELL.value,
            client_order_id="s-1",
        )
        position = await spot_apply_fill(
            db_session,
            order=sell,
            fill_price=Decimal("70000"),
            fill_size=Decimal("0.01"),
        )

        assert position is not None
        assert position.status == PositionStatus.CLOSED.value
        assert position.close_price == Decimal("70000")
        assert position.closed_at is not None
        # PnL: (70000 - 60000) * 0.01 = 100.
        assert position.realized_pnl == Decimal("100.00")

    async def test_sell_orphan_returns_none(self, db_session):
        """SELL with no matching OPEN position — logs and returns None."""
        uid = await _seed_user(db_session)
        sell = await _seed_order(
            db_session,
            user_id=uid,
            side=OrderSide.SELL.value,
            client_order_id="orphan-s",
        )

        result = await spot_apply_fill(
            db_session,
            order=sell,
            fill_price=Decimal("65000"),
            fill_size=Decimal("0.01"),
        )
        assert result is None
        # No position row created.
        from sqlalchemy import select

        rows = (
            (await db_session.execute(select(Position).where(Position.user_id == uid)))
            .scalars()
            .all()
        )
        assert rows == []


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


class TestInputValidation:
    async def test_zero_fill_size_rejected(self, db_session):
        uid = await _seed_user(db_session)
        order = await _seed_order(db_session, user_id=uid)
        with pytest.raises(ValueError, match="fill_size"):
            await spot_apply_fill(
                db_session, order=order, fill_price=Decimal("65000"), fill_size=Decimal("0")
            )

    async def test_negative_fill_price_rejected(self, db_session):
        uid = await _seed_user(db_session)
        order = await _seed_order(db_session, user_id=uid)
        with pytest.raises(ValueError, match="fill_price"):
            await spot_apply_fill(
                db_session,
                order=order,
                fill_price=Decimal("-1"),
                fill_size=Decimal("0.01"),
            )


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


class TestSpotReconcile:
    async def test_venue_says_zero_closes_position(self, db_session):
        """We have OPEN BTC; venue balances show 0 BTC → CLOSE."""
        uid = await _seed_user(db_session)
        buy = await _seed_order(db_session, user_id=uid, client_order_id="b-1")
        await spot_apply_fill(
            db_session,
            order=buy,
            fill_price=Decimal("60000"),
            fill_size=Decimal("0.01"),
        )

        summary = await spot_reconcile_positions(
            db_session,
            user_id=uid,
            venue=Venue.SODEX_SPOT.value,
            venue_balances={},  # venue says no BTC
        )
        assert summary["closed"] == 1

        from sqlalchemy import select

        position = (
            await db_session.execute(select(Position).where(Position.user_id == uid))
        ).scalar_one()
        assert position.status == PositionStatus.CLOSED.value
        # close_price NULL — we don't know it from balance reconcile.
        assert position.close_price is None
        assert position.closed_at is not None

    async def test_orphan_venue_balance_logs_only(self, db_session):
        """Venue has ETH we don't track — log but don't create a row."""
        uid = await _seed_user(db_session)
        summary = await spot_reconcile_positions(
            db_session,
            user_id=uid,
            venue=Venue.SODEX_SPOT.value,
            venue_balances={"ETH": Decimal("0.5")},
        )
        assert summary["orphans"] == 1
        # No position row created.
        from sqlalchemy import select

        rows = (
            (await db_session.execute(select(Position).where(Position.user_id == uid)))
            .scalars()
            .all()
        )
        assert rows == []

    async def test_size_match_is_clean(self, db_session):
        uid = await _seed_user(db_session)
        buy = await _seed_order(db_session, user_id=uid, client_order_id="b-1")
        await spot_apply_fill(
            db_session,
            order=buy,
            fill_price=Decimal("60000"),
            fill_size=Decimal("0.01"),
        )

        summary = await spot_reconcile_positions(
            db_session,
            user_id=uid,
            venue=Venue.SODEX_SPOT.value,
            venue_balances={"BTC": Decimal("0.01")},
        )
        assert summary == {"closed": 0, "orphans": 0, "drift": 0}

    async def test_size_drift_logged_but_not_closed(self, db_session):
        uid = await _seed_user(db_session)
        buy = await _seed_order(db_session, user_id=uid, client_order_id="b-1")
        await spot_apply_fill(
            db_session,
            order=buy,
            fill_price=Decimal("60000"),
            fill_size=Decimal("0.01"),
        )
        # Venue says 0.02 (we say 0.01) — 100% drift.
        summary = await spot_reconcile_positions(
            db_session,
            user_id=uid,
            venue=Venue.SODEX_SPOT.value,
            venue_balances={"BTC": Decimal("0.02")},
        )
        assert summary["drift"] == 1
        assert summary["closed"] == 0

    async def test_paper_positions_untouched(self, db_session):
        """Paper positions don't exist on venue; reconcile MUST NOT close
        them when venue balance is 0."""
        uid = await _seed_user(db_session, paper_trade=True)
        buy = await _seed_order(db_session, user_id=uid, paper_trade=True, client_order_id="pp-1")
        position = await spot_apply_fill(
            db_session,
            order=buy,
            fill_price=Decimal("60000"),
            fill_size=Decimal("0.01"),
        )
        assert position is not None
        position_id = position.id

        # Run reconcile with empty venue balances.
        summary = await spot_reconcile_positions(
            db_session,
            user_id=uid,
            venue=Venue.SODEX_SPOT.value,
            venue_balances={},
        )
        # Paper position MUST NOT count as closed.
        assert summary["closed"] == 0

        # Verify position is still OPEN.
        from sqlalchemy import select

        position = (
            await db_session.execute(select(Position).where(Position.id == position_id))
        ).scalar_one()
        assert position.status == PositionStatus.OPEN.value
