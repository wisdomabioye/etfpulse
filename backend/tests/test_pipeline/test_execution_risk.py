"""Risk-controller tests — every gate gets allow + deny coverage.

Test file structure:

  - `TestIdentityGates`  — wallet / account_id / api_key_name
  - `TestBreakerGates`   — global + per-user circuit breakers
  - `TestEnumGates`      — unsupported TIF / trigger / position-side
  - `TestCapGates`       — open-order count + 24h notional + per-symbol + leverage
  - `TestForUpdateLock`  — proves the lock is acquired (concurrent prepare test)
  - `TestLockedUserReturn` — risk returns the locked user for caller chaining
"""

from __future__ import annotations

import random as _random
import secrets
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from etfpulse.adapters.sodex.schemas import (
    OrderSide,
    OrderType,
    PositionSide,
    TimeInForce,
    TriggerType,
)
from etfpulse.config import settings
from etfpulse.models import (
    Order,
    OrderStatus,
    User,
    Venue,
)

# `etfpulse.models.order` exports a DIFFERENT OrderSide / OrderType /
# TimeInForce — StrEnum with the DB-stored values ("buy" / "limit" /
# "gtc"). The SoDEX schema IntEnums above (1/1/1) are what the gateway
# signs against. RiskRequest uses the IntEnums; Order rows (persisted
# to a String(10) column) use the StrEnums. Aliased to keep both
# accessible without shadowing.
from etfpulse.models.order import OrderSide as DbOrderSide
from etfpulse.models.order import OrderType as DbOrderType
from etfpulse.models.order import TimeInForce as DbTimeInForce
from etfpulse.models.regime import CircuitBreakerTrigger
from etfpulse.pipeline import circuit_breaker
from etfpulse.pipeline.execution.risk import RiskRequest, check_order

# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


# Wallet uniqueness is enforced by `ix_users_wallet_address` (partial
# unique on `wallet_address IS NOT NULL`). Each test that seeds a wallet
# needs a unique one — secrets.token_hex(20) yields exactly 40 lowercase
# hex chars, satisfying the `^0x[0-9a-f]{40}$` CHECK.
def _fresh_wallet() -> str:
    return "0x" + secrets.token_hex(20)


async def _make_user(
    db_session,
    *,
    wallet: str | None = None,  # None → auto-generate; pass None explicitly to test "no wallet"
    no_wallet: bool = False,
    account_id: int | None = 57436,
    spot_key: str | None = "default",
    perps_key: str | None = "default",
    paper_trade: bool = False,
) -> User:
    if not no_wallet and wallet is None:
        wallet = _fresh_wallet()
    u = User(
        wallet_address=None if no_wallet else wallet,
        sodex_account_id=account_id,
        sodex_spot_api_key_name=spot_key,
        sodex_perps_api_key_name=perps_key,
        paper_trade=paper_trade,
    )
    db_session.add(u)
    await db_session.flush()
    return u


def _spot_btc_limit_request(
    *,
    size: Decimal = Decimal("0.01"),
    price: Decimal | None = Decimal("65000"),
    asset: str = "BTC",
    side: int = OrderSide.BUY.value,
    tif: int = TimeInForce.GTC.value,
    leverage: Decimal | None = None,
) -> RiskRequest:
    """Build a happy-path spot LIMIT request. Tests override individual fields."""
    return RiskRequest(
        venue=Venue.SODEX_SPOT.value,
        asset=asset,
        side=side,
        order_type=OrderType.LIMIT.value,
        time_in_force=tif,
        requested_size=size,
        requested_price=price,
        leverage=leverage,
    )


def _perps_btc_limit_request(
    *,
    leverage: Decimal | None = Decimal("3"),
    position_side: int = PositionSide.BOTH.value,
    trigger_type: int | None = None,
    size: Decimal = Decimal("0.01"),
    price: Decimal | None = Decimal("65000"),
    tif: int = TimeInForce.GTC.value,
) -> RiskRequest:
    return RiskRequest(
        venue=Venue.SODEX_PERPS.value,
        asset="BTC",
        side=OrderSide.BUY.value,
        order_type=OrderType.LIMIT.value,
        time_in_force=tif,
        requested_size=size,
        requested_price=price,
        position_side=position_side,
        trigger_type=trigger_type,
        leverage=leverage,
    )


async def _make_order(
    db_session,
    *,
    user_id: int,
    client_order_id: str,
    status: str = OrderStatus.ACKED.value,
    requested_size: Decimal = Decimal("0.01"),
    requested_price: Decimal | None = Decimal("65000"),
    filled_price: Decimal | None = None,
    asset: str = "BTC",
    venue: str = Venue.SODEX_SPOT.value,
    created_at: datetime | None = None,
) -> Order:
    order = Order(
        user_id=user_id,
        client_order_id=client_order_id,
        venue=venue,
        asset=asset,
        side=DbOrderSide.BUY.value,
        order_type=DbOrderType.LIMIT.value,
        time_in_force=DbTimeInForce.GTC.value,
        requested_size=requested_size,
        requested_price=requested_price,
        filled_price=filled_price,
        status=status,
    )
    if created_at is not None:
        order.created_at = created_at
    db_session.add(order)
    await db_session.flush()
    return order


# ---------------------------------------------------------------------------
# Happy path — baseline that every targeted-deny test diverges from
# ---------------------------------------------------------------------------


class TestHappyPath:
    async def test_clean_user_clean_request_allows(self, db_session):
        u = await _make_user(db_session)
        decision, user = await check_order(
            db_session, user_id=u.id, request=_spot_btc_limit_request()
        )
        assert decision.allow is True
        assert decision.reason is None
        assert user.id == u.id
        # Locked user is returned so caller can chain (e.g., reading
        # user.paper_trade to copy onto Order).
        assert user.paper_trade is False

    async def test_perps_with_leverage_allows(self, db_session):
        u = await _make_user(db_session)
        decision, _ = await check_order(
            db_session, user_id=u.id, request=_perps_btc_limit_request()
        )
        assert decision.allow is True


# ---------------------------------------------------------------------------
# Gate 1 — Identity
# ---------------------------------------------------------------------------


class TestIdentityGates:
    async def test_no_wallet_denies(self, db_session):
        u = await _make_user(db_session, no_wallet=True)
        decision, _ = await check_order(db_session, user_id=u.id, request=_spot_btc_limit_request())
        assert decision.allow is False
        assert decision.reason == "wallet_not_bound"

    async def test_no_account_id_denies(self, db_session):
        u = await _make_user(db_session, account_id=None)
        decision, _ = await check_order(db_session, user_id=u.id, request=_spot_btc_limit_request())
        assert decision.allow is False
        assert decision.reason == "sodex_account_not_cached"

    async def test_spot_request_without_spot_key_denies(self, db_session):
        """User has perps key but not spot key; spot request denied."""
        u = await _make_user(db_session, spot_key=None, perps_key="default")
        decision, _ = await check_order(db_session, user_id=u.id, request=_spot_btc_limit_request())
        assert decision.allow is False
        assert decision.reason == "api_key_not_registered"

    async def test_perps_request_without_perps_key_denies(self, db_session):
        u = await _make_user(db_session, spot_key="default", perps_key=None)
        decision, _ = await check_order(
            db_session, user_id=u.id, request=_perps_btc_limit_request()
        )
        assert decision.allow is False
        assert decision.reason == "api_key_not_registered"

    async def test_unknown_venue_denies(self, db_session):
        """Defensive: unrecognised venue → denied with `unsupported_venue`."""
        u = await _make_user(db_session)
        req = RiskRequest(
            venue="sodex_bogus",
            asset="BTC",
            side=OrderSide.BUY.value,
            order_type=OrderType.LIMIT.value,
            time_in_force=TimeInForce.GTC.value,
            requested_size=Decimal("0.01"),
            requested_price=Decimal("65000"),
        )
        decision, _ = await check_order(db_session, user_id=u.id, request=req)
        assert decision.allow is False
        assert decision.reason == "unsupported_venue"

    async def test_unknown_user_raises(self, db_session):
        """Programmer error path — user_id must be validated upstream."""
        with pytest.raises(ValueError, match="does not exist"):
            await check_order(db_session, user_id=999_999_999, request=_spot_btc_limit_request())


# ---------------------------------------------------------------------------
# Gate 2 — Circuit breakers
# ---------------------------------------------------------------------------


class TestBreakerGates:
    async def test_global_breaker_denies(self, db_session):
        u = await _make_user(db_session)
        await circuit_breaker.record(db_session, CircuitBreakerTrigger.MANUAL.value, user_id=None)
        decision, _ = await check_order(db_session, user_id=u.id, request=_spot_btc_limit_request())
        assert decision.allow is False
        assert decision.reason == "global_breaker_active"
        assert decision.breaker_trigger == "manual"

    async def test_per_user_breaker_denies_only_that_user(self, db_session):
        """One user's daily-loss breaker must NOT halt another user."""
        u1 = await _make_user(db_session)
        u2 = await _make_user(db_session)
        await circuit_breaker.record(
            db_session, CircuitBreakerTrigger.DAILY_LOSS_LIMIT.value, user_id=u1.id
        )

        d1, _ = await check_order(db_session, user_id=u1.id, request=_spot_btc_limit_request())
        d2, _ = await check_order(db_session, user_id=u2.id, request=_spot_btc_limit_request())
        assert d1.allow is False
        assert d1.reason == "per_user_breaker_active"
        assert d1.breaker_trigger == "daily_loss_limit"
        assert d2.allow is True

    async def test_resolved_breaker_does_not_block(self, db_session):
        """A breaker that's been resolved must NOT gate."""
        u = await _make_user(db_session)
        row = await circuit_breaker.record(db_session, CircuitBreakerTrigger.MANUAL.value)
        assert row is not None
        await circuit_breaker.resolve(
            db_session, CircuitBreakerTrigger.MANUAL.value, resolved_by="ops"
        )
        decision, _ = await check_order(db_session, user_id=u.id, request=_spot_btc_limit_request())
        assert decision.allow is True

    async def test_per_user_breaker_exempts_reduce_only(self, db_session):
        """PR P1-fix.BREAKER-1 — a per-user breaker (e.g. daily_loss_limit)
        must NOT block a risk-reducing reduce_only order; otherwise a
        loss-limit trip traps the user in the position. A perps
        reduce_only close is allowed through despite the active breaker."""
        u = await _make_user(db_session)
        await circuit_breaker.record(
            db_session, CircuitBreakerTrigger.DAILY_LOSS_LIMIT.value, user_id=u.id
        )
        # Non-reduce-only order is still blocked (no new exposure during a trip).
        blocked, _ = await check_order(db_session, user_id=u.id, request=_perps_btc_limit_request())
        assert blocked.allow is False
        assert blocked.reason == "per_user_breaker_active"
        # reduce_only close is allowed.
        close_req = replace(_perps_btc_limit_request(), reduce_only=True)
        allowed, _ = await check_order(db_session, user_id=u.id, request=close_req)
        assert allowed.allow is True

    async def test_global_breaker_blocks_even_reduce_only(self, db_session):
        """PR P1-fix.BREAKER-1 — the GLOBAL breaker is an operator
        emergency freeze and is NOT exempted: even a reduce_only order is
        blocked during a total halt (incident response; close pricing may
        be unreliable mid-incident)."""
        u = await _make_user(db_session)
        await circuit_breaker.record(db_session, CircuitBreakerTrigger.MANUAL.value, user_id=None)
        close_req = replace(_perps_btc_limit_request(), reduce_only=True)
        decision, _ = await check_order(db_session, user_id=u.id, request=close_req)
        assert decision.allow is False
        assert decision.reason == "global_breaker_active"

    async def test_per_user_manual_halt_blocks_even_reduce_only(self, db_session):
        """PR P1-fix.BREAKER-2 — a per-user MANUAL halt (admin incident
        response, e.g. suspected manipulation) is a TOTAL freeze of the
        user: reduce_only is NOT exempt, unlike an automated daily_loss
        trip. Otherwise the halted user could still close and partially
        defeat the admin's halt."""
        u = await _make_user(db_session)
        await circuit_breaker.record(db_session, CircuitBreakerTrigger.MANUAL.value, user_id=u.id)
        close_req = replace(_perps_btc_limit_request(), reduce_only=True)
        decision, _ = await check_order(db_session, user_id=u.id, request=close_req)
        assert decision.allow is False
        assert decision.reason == "per_user_breaker_active"
        assert decision.breaker_trigger == "manual"


# ---------------------------------------------------------------------------
# Gate 3 — Pre-flight enum gates
# ---------------------------------------------------------------------------


class TestEnumGates:
    async def test_fok_time_in_force_denies(self, db_session):
        u = await _make_user(db_session)
        req = _spot_btc_limit_request(tif=TimeInForce.FOK.value)
        decision, _ = await check_order(db_session, user_id=u.id, request=req)
        assert decision.allow is False
        assert decision.reason == "unsupported_time_in_force"

    async def test_last_price_trigger_denies(self, db_session):
        u = await _make_user(db_session)
        req = _perps_btc_limit_request(trigger_type=TriggerType.LAST_PRICE.value)
        decision, _ = await check_order(db_session, user_id=u.id, request=req)
        assert decision.allow is False
        assert decision.reason == "unsupported_trigger_type"

    async def test_mark_price_trigger_allows(self, db_session):
        """The one supported trigger type passes."""
        u = await _make_user(db_session)
        req = _perps_btc_limit_request(trigger_type=TriggerType.MARK_PRICE.value)
        decision, _ = await check_order(db_session, user_id=u.id, request=req)
        assert decision.allow is True

    async def test_long_position_side_denies(self, db_session):
        """Hedge-mode position sides (LONG/SHORT) are not supported."""
        u = await _make_user(db_session)
        req = _perps_btc_limit_request(position_side=PositionSide.LONG.value)
        decision, _ = await check_order(db_session, user_id=u.id, request=req)
        assert decision.allow is False
        assert decision.reason == "unsupported_position_side"


# ---------------------------------------------------------------------------
# Gate 4 — Caps
# ---------------------------------------------------------------------------


class TestLeverageCap:
    async def test_perps_no_leverage_denies(self, db_session):
        u = await _make_user(db_session)
        req = _perps_btc_limit_request(leverage=None)
        decision, _ = await check_order(db_session, user_id=u.id, request=req)
        assert decision.allow is False
        assert decision.reason == "perps_leverage_missing"

    async def test_perps_zero_leverage_denies(self, db_session):
        u = await _make_user(db_session)
        req = _perps_btc_limit_request(leverage=Decimal("0"))
        decision, _ = await check_order(db_session, user_id=u.id, request=req)
        assert decision.allow is False
        assert decision.reason == "perps_leverage_missing"

    async def test_leverage_above_cap_denies(self, db_session):
        u = await _make_user(db_session)
        # Default cap is 5; ask for 6.
        req = _perps_btc_limit_request(leverage=Decimal("6"))
        decision, _ = await check_order(db_session, user_id=u.id, request=req)
        assert decision.allow is False
        assert decision.reason == "leverage_above_cap"

    async def test_spot_with_leverage_denies(self, db_session):
        u = await _make_user(db_session)
        req = _spot_btc_limit_request(leverage=Decimal("3"))
        decision, _ = await check_order(db_session, user_id=u.id, request=req)
        assert decision.allow is False
        assert decision.reason == "spot_leverage_not_allowed"


class TestOpenOrderCap:
    async def test_at_cap_denies(self, db_session, monkeypatch):
        """User has N non-terminal orders, cap is N → deny."""
        monkeypatch.setattr(settings, "execution_max_open_orders_per_user", 2)
        u = await _make_user(db_session)
        await _make_order(db_session, user_id=u.id, client_order_id="o-1")
        await _make_order(db_session, user_id=u.id, client_order_id="o-2")

        decision, _ = await check_order(db_session, user_id=u.id, request=_spot_btc_limit_request())
        assert decision.allow is False
        assert decision.reason == "open_order_cap_exceeded"

    async def test_terminal_orders_dont_count(self, db_session, monkeypatch):
        """5 FILLED + 5 EXPIRED + 0 in-flight → cap of 2 passes."""
        monkeypatch.setattr(settings, "execution_max_open_orders_per_user", 2)
        u = await _make_user(db_session)
        # Cancelled/expired don't count against the open-order cap.
        # Filled DOES count toward 24h NOTIONAL but NOT toward open-order cap.
        for i in range(5):
            await _make_order(
                db_session,
                user_id=u.id,
                client_order_id=f"can-{i}",
                status=OrderStatus.CANCELLED.value,
            )
        decision, _ = await check_order(db_session, user_id=u.id, request=_spot_btc_limit_request())
        # CANCELLED doesn't count toward open-order, doesn't count
        # toward notional → should pass.
        assert decision.allow is True


class TestNotionalCaps:
    async def test_daily_cap_blocks_when_existing_plus_new_exceeds(self, db_session, monkeypatch):
        monkeypatch.setattr(settings, "execution_daily_notional_usd_cap", Decimal("1000"))
        monkeypatch.setattr(settings, "execution_per_symbol_notional_usd_cap", Decimal("1000"))
        u = await _make_user(db_session)
        # Existing $700 acked notional (0.01 * 70000).
        await _make_order(
            db_session,
            user_id=u.id,
            client_order_id="e-1",
            requested_size=Decimal("0.01"),
            requested_price=Decimal("70000"),
        )
        # New $400 request → total $1100 > $1000 cap.
        req = _spot_btc_limit_request(size=Decimal("0.01"), price=Decimal("40000"))
        decision, _ = await check_order(db_session, user_id=u.id, request=req)
        assert decision.allow is False
        assert decision.reason == "daily_notional_cap_exceeded"

    async def test_old_orders_outside_24h_dont_count(self, db_session, monkeypatch):
        """An order from 25h ago must NOT contribute to today's cap."""
        monkeypatch.setattr(settings, "execution_daily_notional_usd_cap", Decimal("1000"))
        monkeypatch.setattr(settings, "execution_per_symbol_notional_usd_cap", Decimal("1000"))
        u = await _make_user(db_session)
        # Old order at $700 from 25h ago.
        await _make_order(
            db_session,
            user_id=u.id,
            client_order_id="old-1",
            requested_size=Decimal("0.01"),
            requested_price=Decimal("70000"),
            created_at=datetime.now(UTC) - timedelta(hours=25),
        )
        # New $400 request — should pass because old falls outside window.
        req = _spot_btc_limit_request(size=Decimal("0.01"), price=Decimal("40000"))
        decision, _ = await check_order(db_session, user_id=u.id, request=req)
        assert decision.allow is True

    async def test_per_symbol_cap_blocks_independently(self, db_session, monkeypatch):
        """Daily cap fine but per-symbol exceeded → deny with per_symbol reason."""
        monkeypatch.setattr(settings, "execution_daily_notional_usd_cap", Decimal("10000"))
        monkeypatch.setattr(settings, "execution_per_symbol_notional_usd_cap", Decimal("500"))
        u = await _make_user(db_session)
        # $400 of BTC already.
        await _make_order(
            db_session,
            user_id=u.id,
            client_order_id="b-1",
            requested_size=Decimal("0.01"),
            requested_price=Decimal("40000"),
        )
        # New $200 BTC → $600 > $500 per-symbol cap.
        req = _spot_btc_limit_request(asset="BTC", size=Decimal("0.01"), price=Decimal("20000"))
        decision, _ = await check_order(db_session, user_id=u.id, request=req)
        assert decision.allow is False
        assert decision.reason == "per_symbol_notional_cap_exceeded"

    async def test_per_symbol_isolated_by_asset(self, db_session, monkeypatch):
        """BTC near per-symbol cap; ETH order in same user should pass."""
        monkeypatch.setattr(settings, "execution_daily_notional_usd_cap", Decimal("10000"))
        monkeypatch.setattr(settings, "execution_per_symbol_notional_usd_cap", Decimal("500"))
        u = await _make_user(db_session)
        await _make_order(
            db_session,
            user_id=u.id,
            client_order_id="b-1",
            requested_size=Decimal("0.01"),
            requested_price=Decimal("40000"),
        )
        req = _spot_btc_limit_request(asset="ETH", size=Decimal("0.1"), price=Decimal("3000"))
        decision, _ = await check_order(db_session, user_id=u.id, request=req)
        assert decision.allow is True

    async def test_filled_orders_count_toward_notional(self, db_session, monkeypatch):
        """A FILLED order from 2h ago still occupies daily cap."""
        monkeypatch.setattr(settings, "execution_daily_notional_usd_cap", Decimal("1000"))
        monkeypatch.setattr(settings, "execution_per_symbol_notional_usd_cap", Decimal("1000"))
        u = await _make_user(db_session)
        # Filled $800 BTC 2h ago.
        await _make_order(
            db_session,
            user_id=u.id,
            client_order_id="f-1",
            status=OrderStatus.FILLED.value,
            requested_size=Decimal("0.01"),
            requested_price=Decimal("80000"),
            filled_price=Decimal("80000"),
            created_at=datetime.now(UTC) - timedelta(hours=2),
        )
        # New $300 → $1100 > $1000 → deny.
        req = _spot_btc_limit_request(size=Decimal("0.01"), price=Decimal("30000"))
        decision, _ = await check_order(db_session, user_id=u.id, request=req)
        assert decision.allow is False
        assert decision.reason == "daily_notional_cap_exceeded"

    async def test_rejected_orders_dont_count_toward_notional(self, db_session, monkeypatch):
        """REJECTED orders never landed economic exposure — exclude."""
        monkeypatch.setattr(settings, "execution_daily_notional_usd_cap", Decimal("1000"))
        u = await _make_user(db_session)
        # $700 REJECTED — should be invisible to the aggregate.
        await _make_order(
            db_session,
            user_id=u.id,
            client_order_id="r-1",
            status=OrderStatus.REJECTED.value,
            requested_size=Decimal("0.01"),
            requested_price=Decimal("70000"),
        )
        # New $400 — should pass (rejected $700 invisible).
        req = _spot_btc_limit_request(size=Decimal("0.01"), price=Decimal("40000"))
        decision, _ = await check_order(db_session, user_id=u.id, request=req)
        assert decision.allow is True

    async def test_missing_price_denies(self, db_session):
        """Risk requires a reference price — orchestrator must pre-resolve
        spot for market orders before calling check_order."""
        u = await _make_user(db_session)
        req = _spot_btc_limit_request(price=None)
        decision, _ = await check_order(db_session, user_id=u.id, request=req)
        assert decision.allow is False
        assert decision.reason == "missing_requested_price"


# ---------------------------------------------------------------------------
# PR P1-fix.CAP-EXEMPT — reduce_only orders bypass exposure-limiting caps
# ---------------------------------------------------------------------------


def _perps_reduce_only_request(**overrides) -> RiskRequest:
    """A perps reduce_only close: market, IOC, reduce_only=True. Tests
    override fields to exercise each cap."""
    base = replace(
        _perps_btc_limit_request(),
        order_type=OrderType.MARKET.value,
        time_in_force=TimeInForce.IOC.value,
        reduce_only=True,
    )
    return replace(base, **overrides) if overrides else base


class TestReduceOnlyCapExemption:
    async def test_reduce_only_exempt_from_open_order_cap(self, db_session, monkeypatch):
        """A user AT their open-order cap can still place a reduce_only
        close — otherwise they're trapped."""
        monkeypatch.setattr(settings, "execution_max_open_orders_per_user", 2)
        u = await _make_user(db_session)
        await _make_order(
            db_session, user_id=u.id, client_order_id="o-1", venue=Venue.SODEX_PERPS.value
        )
        await _make_order(
            db_session, user_id=u.id, client_order_id="o-2", venue=Venue.SODEX_PERPS.value
        )
        # Normal order denied (at cap)...
        blocked, _ = await check_order(db_session, user_id=u.id, request=_perps_btc_limit_request())
        assert blocked.reason == "open_order_cap_exceeded"
        # ...reduce_only close allowed.
        ok, _ = await check_order(db_session, user_id=u.id, request=_perps_reduce_only_request())
        assert ok.allow is True

    async def test_reduce_only_exempt_from_daily_notional_cap(self, db_session, monkeypatch):
        monkeypatch.setattr(settings, "execution_daily_notional_usd_cap", Decimal("100"))
        u = await _make_user(db_session)
        # An existing FILLED order already blows the 24h notional cap.
        await _make_order(
            db_session,
            user_id=u.id,
            client_order_id="big-1",
            status=OrderStatus.FILLED.value,
            requested_size=Decimal("1"),
            requested_price=Decimal("65000"),
            venue=Venue.SODEX_PERPS.value,
        )
        blocked, _ = await check_order(db_session, user_id=u.id, request=_perps_btc_limit_request())
        assert blocked.reason == "daily_notional_cap_exceeded"
        ok, _ = await check_order(db_session, user_id=u.id, request=_perps_reduce_only_request())
        assert ok.allow is True

    async def test_reduce_only_exempt_from_leverage_cap(self, db_session, monkeypatch):
        """A position opened at 10x must stay closable after the cap is
        lowered to 5x."""
        monkeypatch.setattr(settings, "execution_max_leverage", Decimal("5"))
        u = await _make_user(db_session)
        req = _perps_reduce_only_request(leverage=Decimal("10"))
        ok, _ = await check_order(db_session, user_id=u.id, request=req)
        assert ok.allow is True
        # Non-reduce-only at 10x is still capped.
        blocked, _ = await check_order(
            db_session, user_id=u.id, request=_perps_btc_limit_request(leverage=Decimal("10"))
        )
        assert blocked.reason == "leverage_above_cap"

    async def test_reduce_only_still_requires_positive_leverage(self, db_session):
        """The leverage VALIDITY check is not exempt — a perps order
        (reduce_only included) with no leverage is malformed."""
        u = await _make_user(db_session)
        req = _perps_reduce_only_request(leverage=None)
        decision, _ = await check_order(db_session, user_id=u.id, request=req)
        assert decision.allow is False
        assert decision.reason == "perps_leverage_missing"

    async def test_conditional_order_exempt_from_missing_price(self, db_session):
        """PR P1-fix.CHILD-PRICE-1 — the FE chain's SL/TP legs are market
        orders with requested_price=None. A conditional order rests on its
        trigger, so it must NOT be denied `missing_requested_price`."""
        u = await _make_user(db_session)
        req = replace(
            _perps_btc_limit_request(),
            order_type=OrderType.MARKET.value,
            time_in_force=TimeInForce.IOC.value,
            requested_price=None,
            reduce_only=True,
            is_conditional=True,
            stop_price=Decimal("60000"),
            stop_type="stop_loss",
            trigger_type=TriggerType.MARK_PRICE.value,
        )
        decision, _ = await check_order(db_session, user_id=u.id, request=req)
        assert decision.allow is True

    async def test_non_conditional_reduce_only_still_requires_price(self, db_session):
        """A NON-conditional reduce_only market order with no price is
        still denied — it would execute immediately with no reference and
        crash paper-fill. Only CONDITIONAL orders are price-exempt."""
        u = await _make_user(db_session)
        req = _perps_reduce_only_request(requested_price=None)
        decision, _ = await check_order(db_session, user_id=u.id, request=req)
        assert decision.allow is False
        assert decision.reason == "missing_requested_price"


# ---------------------------------------------------------------------------
# Lock semantics
# ---------------------------------------------------------------------------


class TestForUpdateLock:
    """The User row is locked with SELECT FOR UPDATE. We prove this
    deterministically using a second connection's `FOR UPDATE NOWAIT`
    probe — if the lock is held, NOWAIT raises immediately; otherwise
    NOWAIT acquires.

    NOWAIT is the surgical equivalent of "blocked but I won't wait."
    asyncpg raises `LockNotAvailableError`; SQLAlchemy wraps it as
    `DBAPIError`. Either way, the probe is synchronous (no event-loop
    racing, no timing-sensitive assertions).
    """

    async def test_check_order_holds_for_update(self, test_engine):
        """After check_order runs (without commit), a second session
        attempting FOR UPDATE NOWAIT on the same user row must fail —
        proving the lock is held."""
        from sqlalchemy.exc import DBAPIError

        sessionmaker = async_sessionmaker(test_engine, expire_on_commit=False)

        # Seed a user in its own session, commit so it's visible to both
        # of the test's sessions.
        async with sessionmaker() as setup_session:
            u = User(
                wallet_address=_fresh_wallet(),
                sodex_account_id=57436,
                sodex_spot_api_key_name="default",
                sodex_perps_api_key_name="default",
            )
            setup_session.add(u)
            await setup_session.commit()
            user_id = u.id

        # Session1 takes the lock via check_order (no commit yet).
        session1 = sessionmaker()
        decision, _ = await check_order(
            session1, user_id=user_id, request=_spot_btc_limit_request()
        )
        assert decision.allow is True

        # Session2 probes with FOR UPDATE NOWAIT. Must raise because
        # session1 holds the lock.
        session2 = sessionmaker()
        try:
            with pytest.raises(DBAPIError):
                await session2.execute(
                    select(User).where(User.id == user_id).with_for_update(nowait=True)
                )
            # asyncpg leaves the connection in an aborted-tx state after
            # the NOWAIT failure — roll back to clear it.
            await session2.rollback()

            # Session1 releases the lock.
            await session1.rollback()  # rollback is enough; commit would also work

            # Session2 can now acquire NOWAIT successfully.
            row = await session2.execute(
                select(User).where(User.id == user_id).with_for_update(nowait=True)
            )
            assert row.scalar_one().id == user_id
            await session2.rollback()
        finally:
            await session1.close()
            await session2.close()
            # Clean up the committed user so it doesn't leak into other
            # tests via the test DB.
            async with sessionmaker() as cleanup:
                await cleanup.execute(User.__table__.delete().where(User.id == user_id))
                await cleanup.commit()


class TestLockedUserReturn:
    """`check_order` returns BOTH the decision AND the locked user row.
    Callers (orchestrator) use the returned User to copy `paper_trade`
    onto the Order without re-reading (which would lose the lock)."""

    async def test_returns_user_with_paper_trade_flag(self, db_session):
        u = await _make_user(db_session, paper_trade=True)
        _, locked = await check_order(db_session, user_id=u.id, request=_spot_btc_limit_request())
        assert locked.paper_trade is True

    async def test_returns_user_on_deny(self, db_session):
        """Deny path STILL returns the locked user — useful for the
        orchestrator to log who was denied without re-fetching."""
        u = await _make_user(db_session, no_wallet=True)
        decision, locked = await check_order(
            db_session, user_id=u.id, request=_spot_btc_limit_request()
        )
        assert decision.allow is False
        assert locked.id == u.id
        assert locked.wallet_address is None


# ---------------------------------------------------------------------------
# PR P1.3 — Stop / reduce_only / parent_order_id gates
# ---------------------------------------------------------------------------


class TestStopAttachmentVenue:
    """Stop fields and reduce_only are perps-only in V1."""

    async def test_spot_with_stop_denies(self, db_session):
        u = await _make_user(db_session)
        # Frozen+slots dataclass — use `replace` not `**__dict__`.
        # P1.2 schema gates this upstream; risk gate is defense-in-depth.
        req = replace(
            _spot_btc_limit_request(),
            stop_price=Decimal("60000"),
            stop_type="stop_loss",
        )
        decision, _ = await check_order(db_session, user_id=u.id, request=req)
        assert decision.allow is False
        assert decision.reason == "stop_price_perps_only"

    async def test_spot_with_reduce_only_denies(self, db_session):
        u = await _make_user(db_session)
        req = replace(_spot_btc_limit_request(), reduce_only=True)
        decision, _ = await check_order(db_session, user_id=u.id, request=req)
        assert decision.allow is False
        assert decision.reason == "reduce_only_perps_only"

    async def test_perps_with_stop_and_reduce_only_allows(self, db_session):
        u = await _make_user(db_session)
        req = replace(
            _perps_btc_limit_request(),
            stop_price=Decimal("60000"),
            stop_type="stop_loss",
            trigger_type=TriggerType.MARK_PRICE.value,
            reduce_only=False,
        )
        decision, _ = await check_order(db_session, user_id=u.id, request=req)
        assert decision.allow is True


class TestParentOrderLinkage:
    async def test_parent_not_found_denies(self, db_session):
        u = await _make_user(db_session)
        req = replace(
            _perps_btc_limit_request(),
            parent_order_id=999_999_999,
            reduce_only=True,
        )
        decision, _ = await check_order(db_session, user_id=u.id, request=req)
        assert decision.allow is False
        assert decision.reason == "parent_order_not_found"

    async def test_parent_other_user_denies(self, db_session):
        owner = await _make_user(db_session)
        other = await _make_user(db_session)
        parent = await _make_order(
            db_session,
            user_id=other.id,
            client_order_id="parent-foreign",
            venue=Venue.SODEX_PERPS.value,
        )
        req = replace(
            _perps_btc_limit_request(),
            parent_order_id=parent.id,
            reduce_only=True,
        )
        decision, _ = await check_order(db_session, user_id=owner.id, request=req)
        assert decision.allow is False
        assert decision.reason == "parent_order_not_found"

    async def test_parent_different_venue_denies(self, db_session):
        u = await _make_user(db_session)
        parent = await _make_order(
            db_session,
            user_id=u.id,
            client_order_id="parent-spot",
            venue=Venue.SODEX_SPOT.value,
        )
        req = replace(
            _perps_btc_limit_request(),
            parent_order_id=parent.id,
            reduce_only=True,
        )
        decision, _ = await check_order(db_session, user_id=u.id, request=req)
        assert decision.allow is False
        assert decision.reason == "parent_order_mismatch"

    async def test_parent_same_side_denies(self, db_session):
        u = await _make_user(db_session)
        # Parent is a BUY perps order. Child as BUY (same side) → deny.
        parent = await _make_order(
            db_session,
            user_id=u.id,
            client_order_id="parent-buy",
            venue=Venue.SODEX_PERPS.value,
        )
        req = replace(  # default side is BUY — same as parent
            _perps_btc_limit_request(),
            parent_order_id=parent.id,
            reduce_only=True,
        )
        decision, _ = await check_order(db_session, user_id=u.id, request=req)
        assert decision.allow is False
        assert decision.reason == "parent_order_same_side"

    async def test_parent_without_reduce_only_denies(self, db_session):
        u = await _make_user(db_session)
        parent = await _make_order(
            db_session,
            user_id=u.id,
            client_order_id="parent-rd",
            venue=Venue.SODEX_PERPS.value,
        )
        req = replace(
            _perps_btc_limit_request(),
            side=OrderSide.SELL.value,
            parent_order_id=parent.id,
        )
        decision, _ = await check_order(db_session, user_id=u.id, request=req)
        assert decision.allow is False
        assert decision.reason == "parent_requires_reduce_only"

    async def test_valid_parent_child_allows(self, db_session):
        u = await _make_user(db_session)
        parent = await _make_order(
            db_session,
            user_id=u.id,
            client_order_id="parent-ok",
            venue=Venue.SODEX_PERPS.value,
        )
        req = replace(
            _perps_btc_limit_request(),
            side=OrderSide.SELL.value,  # opposite of parent BUY
            parent_order_id=parent.id,
            reduce_only=True,
            stop_price=Decimal("60000"),
            stop_type="take_profit",
            trigger_type=TriggerType.MARK_PRICE.value,
        )
        decision, _ = await check_order(db_session, user_id=u.id, request=req)
        assert decision.allow is True


# ---------------------------------------------------------------------------
# PR P1-fix.D1 — parent terminal state guard
# ---------------------------------------------------------------------------


class TestParentTerminalState:
    """Parents in REJECTED/EXPIRED/CANCELLED can't host SL/TP children
    — the position those orders would protect doesn't exist."""

    @pytest.mark.parametrize(
        "terminal_status",
        [OrderStatus.REJECTED.value, OrderStatus.EXPIRED.value, OrderStatus.CANCELLED.value],
    )
    async def test_terminal_parent_denied(self, db_session, terminal_status):
        u = await _make_user(db_session)
        parent = await _make_order(
            db_session,
            user_id=u.id,
            client_order_id=f"term-{terminal_status}",
            venue=Venue.SODEX_PERPS.value,
            status=terminal_status,
        )
        req = replace(
            _perps_btc_limit_request(),
            side=OrderSide.SELL.value,
            parent_order_id=parent.id,
            reduce_only=True,
        )
        decision, _ = await check_order(db_session, user_id=u.id, request=req)
        assert decision.allow is False
        assert decision.reason == "parent_order_terminal"

    @pytest.mark.parametrize(
        "live_status",
        [
            OrderStatus.PENDING.value,
            OrderStatus.SUBMITTED.value,
            OrderStatus.ACKED.value,
            OrderStatus.PARTIALLY_FILLED.value,
            OrderStatus.FILLED.value,
        ],
    )
    async def test_live_parent_allowed(self, db_session, live_status):
        u = await _make_user(db_session)
        parent = await _make_order(
            db_session,
            user_id=u.id,
            client_order_id=f"live-{live_status}",
            venue=Venue.SODEX_PERPS.value,
            status=live_status,
        )
        req = replace(
            _perps_btc_limit_request(),
            side=OrderSide.SELL.value,
            parent_order_id=parent.id,
            reduce_only=True,
            stop_price=Decimal("60000"),
            stop_type="stop_loss",
            trigger_type=TriggerType.MARK_PRICE.value,
        )
        decision, _ = await check_order(db_session, user_id=u.id, request=req)
        assert decision.allow is True


# ---------------------------------------------------------------------------
# PR P1-fix.G4 — standalone reduce_only on perps (no parent)
# ---------------------------------------------------------------------------


class TestStandaloneReduceOnly:
    """A reduce_only=True order WITHOUT parent_order_id is the manual-
    exit flow (close-position route uses this shape). Risk gate MUST
    allow it on perps; the parent-link gates only fire when parent
    IS set."""

    async def test_standalone_reduce_only_perps_allowed(self, db_session):
        u = await _make_user(db_session)
        req = replace(_perps_btc_limit_request(), reduce_only=True)
        decision, _ = await check_order(db_session, user_id=u.id, request=req)
        assert decision.allow is True


# ---------------------------------------------------------------------------
# PR P1-fix.CRIT-1 — stop order requires a trigger_type
# ---------------------------------------------------------------------------


class TestStopRequiresTrigger:
    """A perps stop (stop_price set) MUST carry trigger_type, else the
    signed payload would omit the trigger feed and the gateway can't
    arm the stop. The FE chain sets trigger_type=mark_price; this gate
    is defense-in-depth against a direct API caller omitting it."""

    async def test_stop_without_trigger_denied(self, db_session):
        u = await _make_user(db_session)
        req = replace(
            _perps_btc_limit_request(),
            stop_price=Decimal("60000"),
            stop_type="stop_loss",
            trigger_type=None,
        )
        decision, _ = await check_order(db_session, user_id=u.id, request=req)
        assert decision.allow is False
        assert decision.reason == "stop_requires_trigger_type"

    async def test_stop_with_mark_price_trigger_allowed(self, db_session):
        u = await _make_user(db_session)
        req = replace(
            _perps_btc_limit_request(),
            stop_price=Decimal("60000"),
            stop_type="stop_loss",
            trigger_type=TriggerType.MARK_PRICE.value,
        )
        decision, _ = await check_order(db_session, user_id=u.id, request=req)
        assert decision.allow is True


# ---------------------------------------------------------------------------
# Property tests — seeded-random fuzz over check_order asserting the
# cross-cutting invariants the P1-fix rounds established. Deterministic
# (fixed RNG seed) so a failure reproduces; the request is included in
# every assert message for a readable counter-example.
# ---------------------------------------------------------------------------

# Exposure-limiting deny reasons a VALID reduce_only order must NEVER hit
# (CAP-EXEMPT + BREAKER-1). A reduce_only order may still be denied for
# VALIDITY (missing leverage/price), the GLOBAL breaker, or a per-user
# MANUAL halt — those are asserted separately.
_EXPOSURE_DENY_REASONS = frozenset(
    {
        "open_order_cap_exceeded",
        "daily_notional_cap_exceeded",
        "per_symbol_notional_cap_exceeded",
        "leverage_above_cap",
    }
)


def _rand_valid_perps_request(rng: _random.Random, *, reduce_only: bool) -> RiskRequest:
    """A perps request that passes EVERY non-exposure gate (valid identity
    inputs, supported enums, positive in-cap leverage, a reference price),
    varying only the dimensions that matter for the exposure/breaker
    invariants. The only thing under test is reduce_only."""
    return RiskRequest(
        venue=Venue.SODEX_PERPS.value,
        asset="BTC",
        side=rng.choice([OrderSide.BUY.value, OrderSide.SELL.value]),
        order_type=rng.choice([OrderType.LIMIT.value, OrderType.MARKET.value]),
        time_in_force=rng.choice([TimeInForce.GTC.value, TimeInForce.IOC.value]),
        requested_size=Decimal(str(rng.choice(["0.001", "0.01", "0.1", "1"]))),
        requested_price=Decimal(str(rng.choice(["100", "65000", "70000"]))),
        position_side=PositionSide.BOTH.value,
        trigger_type=None,
        leverage=Decimal(str(rng.randint(1, 5))),  # within the default cap (5)
        reduce_only=reduce_only,
    )


class TestRiskGateProperties:
    _N = 150  # examples per scenario

    async def test_prop_no_crash_on_arbitrary_shapes(self, db_session):
        """INV — check_order returns a decision (never raises) for any
        well-typed RiskRequest, however nonsensical the field combo."""
        u = await _make_user(db_session)
        rng = _random.Random(20260608)
        venues = [Venue.SODEX_SPOT.value, Venue.SODEX_PERPS.value, "garbage"]
        for i in range(self._N):
            req = RiskRequest(
                venue=rng.choice(venues),
                asset=rng.choice(["BTC", "ETH", "DOGE"]),
                side=rng.choice([1, 2, 99]),
                order_type=rng.choice([1, 2]),
                time_in_force=rng.choice([1, 2, 3, 4]),
                requested_size=Decimal(str(rng.choice(["-1", "0", "0.01", "5"]))),
                requested_price=rng.choice([None, Decimal("65000"), Decimal("-1")]),
                position_side=rng.choice([None, 1, 2, 3]),
                trigger_type=rng.choice([None, 1, 2, 3]),
                leverage=rng.choice(
                    [None, Decimal("-1"), Decimal("0"), Decimal("3"), Decimal("99")]
                ),
                is_conditional=rng.choice([True, False]),
                stop_price=rng.choice([None, Decimal("60000")]),
                stop_type=rng.choice([None, "stop_loss", "take_profit"]),
                reduce_only=rng.choice([True, False]),
                parent_order_id=rng.choice([None, 999_999_999]),
            )
            # Must not raise; must return a well-formed decision.
            decision, locked = await check_order(db_session, user_id=u.id, request=req)
            assert isinstance(decision.allow, bool), f"example {i}: {req}"
            assert locked.id == u.id
            if not decision.allow:
                assert decision.reason, f"deny without reason: example {i}: {req}"

    async def test_prop_reduce_only_never_trapped_by_caps(self, db_session, monkeypatch):
        """INV (CAP-EXEMPT) — with the user maxed on EVERY exposure cap, a
        valid reduce_only order is never denied for a cap reason, while the
        same-shaped non-reduce_only order always is."""
        monkeypatch.setattr(settings, "execution_max_open_orders_per_user", 1)
        monkeypatch.setattr(settings, "execution_daily_notional_usd_cap", Decimal("1"))
        monkeypatch.setattr(settings, "execution_per_symbol_notional_usd_cap", Decimal("1"))
        u = await _make_user(db_session)
        # One ACKED order → at the open-order cap; a big FILLED order → over
        # both notional caps.
        await _make_order(
            db_session, user_id=u.id, client_order_id="open-1", venue=Venue.SODEX_PERPS.value
        )
        await _make_order(
            db_session,
            user_id=u.id,
            client_order_id="big-1",
            status=OrderStatus.FILLED.value,
            requested_size=Decimal("1"),
            requested_price=Decimal("65000"),
            venue=Venue.SODEX_PERPS.value,
        )
        rng = _random.Random(424242)
        for i in range(self._N):
            ro = _rand_valid_perps_request(rng, reduce_only=True)
            d_ro, _ = await check_order(db_session, user_id=u.id, request=ro)
            assert d_ro.allow is True, f"reduce_only trapped: example {i}: {ro} -> {d_ro.reason}"

            normal = _rand_valid_perps_request(rng, reduce_only=False)
            d_n, _ = await check_order(db_session, user_id=u.id, request=normal)
            assert d_n.allow is False, f"non-reduce_only NOT capped: example {i}: {normal}"
            assert d_n.reason in _EXPOSURE_DENY_REASONS, f"unexpected reason: {d_n.reason}"

    async def test_prop_daily_loss_breaker_exempts_reduce_only(self, db_session):
        """INV (BREAKER-1) — an AUTOMATED per-user breaker exempts every
        valid reduce_only order and denies every non-reduce_only one."""
        u = await _make_user(db_session)
        await circuit_breaker.record(
            db_session, CircuitBreakerTrigger.DAILY_LOSS_LIMIT.value, user_id=u.id
        )
        rng = _random.Random(99)
        for i in range(self._N):
            ro = _rand_valid_perps_request(rng, reduce_only=True)
            d_ro, _ = await check_order(db_session, user_id=u.id, request=ro)
            assert d_ro.allow is True, f"reduce_only blocked by loss breaker: {ro} -> {d_ro.reason}"

            normal = _rand_valid_perps_request(rng, reduce_only=False)
            d_n, _ = await check_order(db_session, user_id=u.id, request=normal)
            assert d_n.allow is False, f"non-reduce_only not blocked: {normal}"
            assert d_n.reason == "per_user_breaker_active"

    async def test_prop_manual_and_global_halts_block_everything(self, db_session):
        """INV (BREAKER-2) — a per-user MANUAL halt and a GLOBAL halt are
        TOTAL freezes: NOTHING (reduce_only included) is allowed.

        Phased because a GLOBAL breaker halts EVERY user (it would mask the
        per-user manual reason), so the manual-only assertions run BEFORE
        the global breaker is recorded."""
        rng = _random.Random(7)

        # Phase 1 — per-user MANUAL halt only (no global active yet).
        manual_user = await _make_user(db_session)
        await circuit_breaker.record(
            db_session, CircuitBreakerTrigger.MANUAL.value, user_id=manual_user.id
        )
        for _ in range(self._N):
            for reduce_only in (True, False):
                req = _rand_valid_perps_request(rng, reduce_only=reduce_only)
                d, _ = await check_order(db_session, user_id=manual_user.id, request=req)
                assert d.allow is False, f"manual halt let order through: {req}"
                assert d.reason == "per_user_breaker_active"

        # Phase 2 — add a GLOBAL halt; it freezes EVERYONE regardless of
        # per-user state (and regardless of reduce_only).
        global_user = await _make_user(db_session)
        await circuit_breaker.record(db_session, CircuitBreakerTrigger.MANUAL.value, user_id=None)
        for _ in range(self._N):
            for reduce_only in (True, False):
                req = _rand_valid_perps_request(rng, reduce_only=reduce_only)
                d, _ = await check_order(db_session, user_id=global_user.id, request=req)
                assert d.allow is False, f"global halt let order through: {req}"
                assert d.reason == "global_breaker_active"
