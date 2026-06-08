"""Tests for `pipeline.execution.positions_perps`.

- apply_fill: open LONG, open SHORT, extend, reduce, close, flip excess.
- reconcile: closed-via-reconcile (liquidation/external), orphan,
  side-mismatch, drift, untyped-row skip, paper isolation.
"""

from __future__ import annotations

import secrets
from decimal import Decimal

import pytest
from sqlalchemy import select

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
from etfpulse.pipeline.execution.positions_perps import (
    _normalise_venue_position,
    perps_apply_fill,
    perps_reconcile_positions,
)


def _wallet() -> str:
    return "0x" + secrets.token_hex(20)


async def _seed_user(db_session, *, paper_trade: bool = False) -> int:
    u = User(
        wallet_address=_wallet(),
        sodex_account_id=57436,
        sodex_perps_api_key_name="default",
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
    reduce_only: bool = False,
) -> Order:
    co_id = client_order_id or f"o-{secrets.token_hex(4)}"
    o = Order(
        user_id=user_id,
        client_order_id=co_id,
        venue=Venue.SODEX_PERPS.value,
        asset=asset,
        side=side,
        order_type=OrderType.LIMIT.value,
        time_in_force=TimeInForce.GTC.value,
        requested_size=Decimal("0.01"),
        requested_price=Decimal("65000"),
        status=OrderStatus.FILLED.value,
        paper_trade=paper_trade,
        reduce_only=reduce_only,
    )
    db_session.add(o)
    await db_session.flush()
    return o


# ---------------------------------------------------------------------------
# apply_fill — open
# ---------------------------------------------------------------------------


class TestPerpsOpen:
    async def test_buy_first_opens_long(self, db_session):
        uid = await _seed_user(db_session)
        order = await _seed_order(db_session, user_id=uid)

        position = await perps_apply_fill(
            db_session,
            order=order,
            fill_price=Decimal("65000"),
            fill_size=Decimal("0.01"),
        )
        assert position is not None
        assert position.side == PositionSide.LONG.value
        assert position.venue == Venue.SODEX_PERPS.value

    async def test_sell_first_opens_short(self, db_session):
        uid = await _seed_user(db_session)
        order = await _seed_order(db_session, user_id=uid, side=OrderSide.SELL.value)

        position = await perps_apply_fill(
            db_session,
            order=order,
            fill_price=Decimal("65000"),
            fill_size=Decimal("0.01"),
        )
        assert position is not None
        assert position.side == PositionSide.SHORT.value


class TestPerpsReduceOnlyGuard:
    """PR P1-fix.RO-FILL-1 — a reduce_only fill can ONLY reduce/close. It
    must never open (no position) or extend (same-side), else it creates
    exposure — defeating both reduce-only semantics and the CAP-EXEMPT
    safety invariant (reduce_only bypasses the exposure caps)."""

    async def test_reduce_only_no_position_is_noop(self, db_session):
        uid = await _seed_user(db_session)
        order = await _seed_order(
            db_session, user_id=uid, side=OrderSide.SELL.value, reduce_only=True
        )
        position = await perps_apply_fill(
            db_session, order=order, fill_price=Decimal("65000"), fill_size=Decimal("0.01")
        )
        assert position is None  # nothing opened
        rows = (
            (await db_session.execute(select(Position).where(Position.user_id == uid)))
            .scalars()
            .all()
        )
        assert rows == []

    async def test_reduce_only_same_side_is_noop(self, db_session):
        uid = await _seed_user(db_session)
        # Open a LONG with a normal BUY.
        open_order = await _seed_order(db_session, user_id=uid, client_order_id="open-1")
        await perps_apply_fill(
            db_session, order=open_order, fill_price=Decimal("60000"), fill_size=Decimal("0.02")
        )
        # reduce_only BUY (same side as the LONG) would EXTEND — must no-op.
        ro_buy = await _seed_order(
            db_session,
            user_id=uid,
            side=OrderSide.BUY.value,
            reduce_only=True,
            client_order_id="ro-1",
        )
        position = await perps_apply_fill(
            db_session, order=ro_buy, fill_price=Decimal("70000"), fill_size=Decimal("0.01")
        )
        assert position is None
        existing = (
            await db_session.execute(select(Position).where(Position.user_id == uid))
        ).scalar_one()
        assert existing.size == Decimal("0.02")  # unchanged — not extended

    async def test_reduce_only_opposite_side_reduces(self, db_session):
        uid = await _seed_user(db_session)
        open_order = await _seed_order(db_session, user_id=uid, client_order_id="open-1")
        await perps_apply_fill(
            db_session, order=open_order, fill_price=Decimal("60000"), fill_size=Decimal("0.02")
        )
        # reduce_only SELL (opposite the LONG) reduces normally.
        ro_sell = await _seed_order(
            db_session,
            user_id=uid,
            side=OrderSide.SELL.value,
            reduce_only=True,
            client_order_id="ro-2",
        )
        position = await perps_apply_fill(
            db_session, order=ro_sell, fill_price=Decimal("65000"), fill_size=Decimal("0.01")
        )
        assert position is not None
        assert position.size == Decimal("0.01")  # reduced from 0.02


class TestPerpsExtend:
    async def test_same_side_aggregates_avg(self, db_session):
        uid = await _seed_user(db_session)
        order1 = await _seed_order(db_session, user_id=uid, client_order_id="b-1")
        await perps_apply_fill(
            db_session,
            order=order1,
            fill_price=Decimal("60000"),
            fill_size=Decimal("0.01"),
        )
        order2 = await _seed_order(db_session, user_id=uid, client_order_id="b-2")
        position = await perps_apply_fill(
            db_session,
            order=order2,
            fill_price=Decimal("70000"),
            fill_size=Decimal("0.01"),
        )
        assert position is not None
        assert position.size == Decimal("0.02")
        assert position.entry_price == Decimal("65000.00000000")


class TestPerpsReduce:
    async def test_long_reduced_by_partial_sell(self, db_session):
        uid = await _seed_user(db_session)
        open_order = await _seed_order(db_session, user_id=uid, client_order_id="b-1")
        await perps_apply_fill(
            db_session,
            order=open_order,
            fill_price=Decimal("60000"),
            fill_size=Decimal("0.02"),
        )
        reduce_order = await _seed_order(
            db_session,
            user_id=uid,
            side=OrderSide.SELL.value,
            client_order_id="s-1",
        )
        position = await perps_apply_fill(
            db_session,
            order=reduce_order,
            fill_price=Decimal("65000"),
            fill_size=Decimal("0.01"),
        )
        assert position is not None
        assert position.status == PositionStatus.OPEN.value
        assert position.size == Decimal("0.01")
        # PnL: LONG reduced — (65000-60000) * 0.01 = 50.
        assert position.realized_pnl == Decimal("50.00")

    async def test_short_reduced_by_partial_buy(self, db_session):
        """SHORT position closing with BUY: PnL direction inverted."""
        uid = await _seed_user(db_session)
        open_order = await _seed_order(
            db_session, user_id=uid, side=OrderSide.SELL.value, client_order_id="s-1"
        )
        await perps_apply_fill(
            db_session,
            order=open_order,
            fill_price=Decimal("65000"),
            fill_size=Decimal("0.02"),
        )
        reduce_order = await _seed_order(
            db_session,
            user_id=uid,
            side=OrderSide.BUY.value,
            client_order_id="b-1",
        )
        position = await perps_apply_fill(
            db_session,
            order=reduce_order,
            fill_price=Decimal("60000"),
            fill_size=Decimal("0.01"),
        )
        assert position is not None
        assert position.side == PositionSide.SHORT.value
        # PnL: SHORT reduced with price moving DOWN = profit.
        # (65000-60000) * 0.01 = 50.
        assert position.realized_pnl == Decimal("50.00")

    async def test_full_close_long(self, db_session):
        uid = await _seed_user(db_session)
        open_order = await _seed_order(db_session, user_id=uid, client_order_id="b-1")
        await perps_apply_fill(
            db_session,
            order=open_order,
            fill_price=Decimal("60000"),
            fill_size=Decimal("0.01"),
        )
        close_order = await _seed_order(
            db_session,
            user_id=uid,
            side=OrderSide.SELL.value,
            client_order_id="s-1",
        )
        position = await perps_apply_fill(
            db_session,
            order=close_order,
            fill_price=Decimal("70000"),
            fill_size=Decimal("0.01"),
        )
        assert position is not None
        assert position.status == PositionStatus.CLOSED.value
        assert position.close_price == Decimal("70000")
        assert position.realized_pnl == Decimal("100.00")


class TestPerpsFlipExcess:
    async def test_flip_excess_logged_not_opened(self, db_session):
        """Closing more than we hold — V1 closes the matched portion
        and logs the excess (does NOT open opposite-side position)."""
        uid = await _seed_user(db_session)
        open_order = await _seed_order(db_session, user_id=uid, client_order_id="b-1")
        await perps_apply_fill(
            db_session,
            order=open_order,
            fill_price=Decimal("60000"),
            fill_size=Decimal("0.01"),
        )
        # Try to close 0.05 (5x more than we hold).
        close_order = await _seed_order(
            db_session,
            user_id=uid,
            side=OrderSide.SELL.value,
            client_order_id="s-excess",
        )
        position = await perps_apply_fill(
            db_session,
            order=close_order,
            fill_price=Decimal("70000"),
            fill_size=Decimal("0.05"),
        )
        # Position is CLOSED (matched 0.01); excess 0.04 logged.
        assert position is not None
        assert position.status == PositionStatus.CLOSED.value
        # PnL only on the matched 0.01: (70000-60000)*0.01 = 100.
        assert position.realized_pnl == Decimal("100.00")

        # No second (SHORT) position was opened.
        from sqlalchemy import select

        rows = (
            (await db_session.execute(select(Position).where(Position.user_id == uid)))
            .scalars()
            .all()
        )
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


class TestInputValidation:
    async def test_zero_fill_size_rejected(self, db_session):
        uid = await _seed_user(db_session)
        order = await _seed_order(db_session, user_id=uid)
        with pytest.raises(ValueError, match="fill_size"):
            await perps_apply_fill(
                db_session,
                order=order,
                fill_price=Decimal("65000"),
                fill_size=Decimal("0"),
            )

    async def test_negative_fill_price_rejected(self, db_session):
        uid = await _seed_user(db_session)
        order = await _seed_order(db_session, user_id=uid)
        with pytest.raises(ValueError, match="fill_price"):
            await perps_apply_fill(
                db_session,
                order=order,
                fill_price=Decimal("-1"),
                fill_size=Decimal("0.01"),
            )


# ---------------------------------------------------------------------------
# _normalise_venue_position — defensive shape parsing
# ---------------------------------------------------------------------------


class TestNormaliseVenuePosition:
    def test_canonical_shape(self):
        row = {"asset": "BTC", "side": "LONG", "size": "0.01"}
        assert _normalise_venue_position(row) == ("BTC", "long", Decimal("0.01"))

    def test_vbtc_vusdc_symbol_extracted(self):
        row = {"symbol": "vBTC_vUSDC", "side": "LONG", "size": "0.5"}
        assert _normalise_venue_position(row) == ("BTC", "long", Decimal("0.5"))

    def test_int_position_side(self):
        """schema.md PositionSide: 1=LONG_or_BOTH, 2=LONG, 3=SHORT.
        Here we test the documented LONG mapping (1) + SHORT (2)."""
        assert _normalise_venue_position({"asset": "BTC", "side": 1, "size": "0.5"}) == (
            "BTC",
            "long",
            Decimal("0.5"),
        )
        assert _normalise_venue_position({"asset": "BTC", "side": 2, "size": "0.5"}) == (
            "BTC",
            "short",
            Decimal("0.5"),
        )

    def test_short_keys_accepted(self):
        row = {"s": "vETH_vUSDC", "S": "SHORT", "q": "1.5"}
        assert _normalise_venue_position(row) == ("ETH", "short", Decimal("1.5"))

    @pytest.mark.parametrize(
        "row",
        [
            {},  # empty
            {"asset": "BTC"},  # missing side+size
            {"asset": "BTC", "side": "LONG"},  # missing size
            {"side": "LONG", "size": "0.01"},  # missing asset
            {"asset": "", "side": "LONG", "size": "0.01"},  # empty asset
            {"asset": "BTC", "side": "WEIRD", "size": "0.01"},  # bad side
            {"asset": "BTC", "side": "LONG", "size": "not-a-number"},  # bad size
        ],
    )
    def test_unparseable_returns_none(self, row):
        assert _normalise_venue_position(row) is None


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


class TestPerpsReconcile:
    async def test_venue_missing_position_closes_ours(self, db_session):
        """We have OPEN LONG BTC; venue says no BTC position → CLOSED."""
        uid = await _seed_user(db_session)
        order = await _seed_order(db_session, user_id=uid, client_order_id="b-1")
        await perps_apply_fill(
            db_session,
            order=order,
            fill_price=Decimal("60000"),
            fill_size=Decimal("0.01"),
        )

        summary = await perps_reconcile_positions(
            db_session,
            user_id=uid,
            venue=Venue.SODEX_PERPS.value,
            venue_positions=[],
        )
        assert summary["closed"] == 1

        from sqlalchemy import select

        position = (
            await db_session.execute(select(Position).where(Position.user_id == uid))
        ).scalar_one()
        assert position.status == PositionStatus.CLOSED.value

    async def test_size_drift_logged_not_mutated(self, db_session):
        uid = await _seed_user(db_session)
        order = await _seed_order(db_session, user_id=uid, client_order_id="b-1")
        await perps_apply_fill(
            db_session,
            order=order,
            fill_price=Decimal("60000"),
            fill_size=Decimal("0.01"),
        )

        # Venue says we have 0.02 BTC long — 100% drift.
        summary = await perps_reconcile_positions(
            db_session,
            user_id=uid,
            venue=Venue.SODEX_PERPS.value,
            venue_positions=[{"asset": "BTC", "side": "LONG", "size": "0.02"}],
        )
        assert summary["drift"] == 1
        assert summary["closed"] == 0

    async def test_side_mismatch_logged(self, db_session):
        """We say LONG; venue says SHORT — severe drift, log only."""
        uid = await _seed_user(db_session)
        order = await _seed_order(db_session, user_id=uid, client_order_id="b-1")
        await perps_apply_fill(
            db_session,
            order=order,
            fill_price=Decimal("60000"),
            fill_size=Decimal("0.01"),
        )

        summary = await perps_reconcile_positions(
            db_session,
            user_id=uid,
            venue=Venue.SODEX_PERPS.value,
            venue_positions=[{"asset": "BTC", "side": "SHORT", "size": "0.01"}],
        )
        assert summary["side_mismatch"] == 1

    async def test_orphan_from_venue(self, db_session):
        """Venue has SOL we don't track → orphan log."""
        uid = await _seed_user(db_session)
        summary = await perps_reconcile_positions(
            db_session,
            user_id=uid,
            venue=Venue.SODEX_PERPS.value,
            venue_positions=[{"asset": "SOL", "side": "LONG", "size": "1.0"}],
        )
        assert summary["orphans"] == 1

    async def test_unparseable_row_skipped(self, db_session):
        uid = await _seed_user(db_session)
        summary = await perps_reconcile_positions(
            db_session,
            user_id=uid,
            venue=Venue.SODEX_PERPS.value,
            venue_positions=[
                {"weird": "row"},
                {"asset": "BTC", "side": "LONG", "size": "0.01"},  # valid → orphan
            ],
        )
        assert summary["skipped"] == 1
        assert summary["orphans"] == 1

    async def test_clean_match(self, db_session):
        uid = await _seed_user(db_session)
        order = await _seed_order(db_session, user_id=uid, client_order_id="b-1")
        await perps_apply_fill(
            db_session,
            order=order,
            fill_price=Decimal("60000"),
            fill_size=Decimal("0.01"),
        )

        summary = await perps_reconcile_positions(
            db_session,
            user_id=uid,
            venue=Venue.SODEX_PERPS.value,
            venue_positions=[{"asset": "BTC", "side": "LONG", "size": "0.01"}],
        )
        assert summary == {
            "closed": 0,
            "orphans": 0,
            "drift": 0,
            "side_mismatch": 0,
            "skipped": 0,
        }

    async def test_paper_positions_untouched(self, db_session):
        """Paper positions don't exist on venue; reconcile MUST NOT
        close them when venue list is empty."""
        uid = await _seed_user(db_session, paper_trade=True)
        order = await _seed_order(db_session, user_id=uid, paper_trade=True, client_order_id="pp-1")
        await perps_apply_fill(
            db_session,
            order=order,
            fill_price=Decimal("60000"),
            fill_size=Decimal("0.01"),
        )

        summary = await perps_reconcile_positions(
            db_session,
            user_id=uid,
            venue=Venue.SODEX_PERPS.value,
            venue_positions=[],
        )
        assert summary["closed"] == 0
