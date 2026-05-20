"""Focused tests for the PR D.3.1 hardening fixes.

Each test pins ONE invariant introduced by the D.3 review pass so a
future regression fails loud:

  - Spot over-sell PnL bug (P0 #1): SELL fill_size > existing.size must
    not over-count realised PnL.
  - SodexAuthError terminalization (P0 #5): submit_new transitions to
    REJECTED, not stuck SUBMITTED.
  - Cancel idempotency guard (P1 #6): second submit_cancel must
    short-circuit if signature is already set.
  - extract_asset output validation (P2 #15): malformed symbol names
    raise instead of silently producing an empty asset.
  - circuit_breaker.record race-safety (P1 #9): partial unique index
    prevents duplicate active rows.
  - circuit_breaker.find_active (new helper, P1 #10).
  - config.execution_per_symbol_notional_usd_cap > 0 (P1 #13).

The cancel-signature retry test exercises the in-flight idempotency
that prevents nonce-replay-rejection on user double-click.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from etfpulse.adapters.sodex import SodexAuthError, SodexPerpsClient, SodexSpotClient
from etfpulse.config import Settings
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
from etfpulse.models.regime import CircuitBreaker, CircuitBreakerTrigger
from etfpulse.pipeline import circuit_breaker
from etfpulse.pipeline.execution.pipeline import submit_cancel, submit_new
from etfpulse.pipeline.execution.positions_spot import spot_apply_fill
from etfpulse.pipeline.symbols_refresh import extract_asset_from_symbol_name

_VALID_SIG = "0x01" + "ab" * 65


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
    status: str = OrderStatus.PENDING.value,
    venue: str = Venue.SODEX_SPOT.value,
    client_order_id: str | None = None,
    eip712_payload: str | None = None,
    eip712_signature: str | None = None,
    eip712_payload_hash: str | None = None,
    nonce: int | None = None,
    nonce_expires_at: datetime | None = None,
    cancel_eip712_payload: str | None = None,
    cancel_eip712_signature: str | None = None,
    cancel_nonce: int | None = None,
) -> Order:
    o = Order(
        user_id=user_id,
        client_order_id=client_order_id or f"o-{secrets.token_hex(4)}",
        venue=venue,
        asset="BTC",
        side=OrderSide.BUY.value,
        order_type=OrderType.LIMIT.value,
        time_in_force=TimeInForce.GTC.value,
        requested_size=Decimal("0.01"),
        requested_price=Decimal("65000"),
        status=status,
        eip712_payload=eip712_payload,
        eip712_signature=eip712_signature,
        eip712_payload_hash=eip712_payload_hash,
        nonce=nonce,
        nonce_expires_at=nonce_expires_at,
        cancel_eip712_payload=cancel_eip712_payload,
        cancel_eip712_signature=cancel_eip712_signature,
        cancel_nonce=cancel_nonce,
    )
    db_session.add(o)
    await db_session.flush()
    return o


# ---------------------------------------------------------------------------
# P0 #1 — spot over-sell PnL
# ---------------------------------------------------------------------------


class TestSpotOverSellPnlFix:
    async def test_over_sell_matches_only_held_size(self, db_session):
        """SELL fill_size > existing.size: PnL counts only matched_size,
        Position closes with non-negative residual + excess logged."""
        u = await _seed_user(db_session)

        # Open with 0.01 BTC @ 60000.
        buy_order = Order(
            user_id=u.id,
            client_order_id="open-1",
            venue=Venue.SODEX_SPOT.value,
            asset="BTC",
            side=OrderSide.BUY.value,
            order_type=OrderType.LIMIT.value,
            time_in_force=TimeInForce.GTC.value,
            requested_size=Decimal("0.01"),
            requested_price=Decimal("60000"),
            status=OrderStatus.FILLED.value,
        )
        db_session.add(buy_order)
        await db_session.flush()
        await spot_apply_fill(
            db_session,
            order=buy_order,
            fill_price=Decimal("60000"),
            fill_size=Decimal("0.01"),
        )

        # Over-sell: SELL 0.05 against a 0.01 position.
        sell_order = Order(
            user_id=u.id,
            client_order_id="oversell-1",
            venue=Venue.SODEX_SPOT.value,
            asset="BTC",
            side=OrderSide.SELL.value,
            order_type=OrderType.LIMIT.value,
            time_in_force=TimeInForce.GTC.value,
            requested_size=Decimal("0.05"),
            requested_price=Decimal("70000"),
            status=OrderStatus.FILLED.value,
        )
        db_session.add(sell_order)
        await db_session.flush()

        position = await spot_apply_fill(
            db_session,
            order=sell_order,
            fill_price=Decimal("70000"),
            fill_size=Decimal("0.05"),
        )
        assert position is not None
        assert position.status == PositionStatus.CLOSED.value
        # PnL = (70000 - 60000) * 0.01 = 100, NOT 500.
        assert position.realized_pnl == Decimal("100.00")


# ---------------------------------------------------------------------------
# P2 #15 — extract_asset output validation
# ---------------------------------------------------------------------------


class TestExtractAssetValidation:
    @pytest.mark.parametrize(
        "name",
        [
            "vBTC_vUSDC",
            "ETH_USDC",
            "vvSOL_X",
        ],
    )
    def test_well_formed(self, name):
        result = extract_asset_from_symbol_name(name)
        assert result and result != ""

    def test_empty_input_raises(self):
        with pytest.raises(ValueError, match="empty name"):
            extract_asset_from_symbol_name("")

    @pytest.mark.parametrize(
        "name",
        [
            "_USDC",  # empty base before underscore
        ],
    )
    def test_empty_output_raises(self, name):
        with pytest.raises(ValueError, match="empty asset"):
            extract_asset_from_symbol_name(name)


# ---------------------------------------------------------------------------
# P1 #13 — execution_per_symbol_notional_usd_cap gt=0
# ---------------------------------------------------------------------------


class TestPerSymbolCapValidation:
    def test_zero_rejected(self, monkeypatch):
        monkeypatch.setenv("EXECUTION_PER_SYMBOL_NOTIONAL_USD_CAP", "0")
        with pytest.raises(ValidationError):
            Settings()


# ---------------------------------------------------------------------------
# P1 #9 — circuit_breaker.record race-safety (partial unique enforced by DB)
# ---------------------------------------------------------------------------


class TestCircuitBreakerRaceSafety:
    async def test_record_returns_existing_row_on_conflict(self, db_session):
        """ON CONFLICT DO NOTHING: second record() for same scope returns
        None (caller's invariant unchanged). The DB enforces uniqueness
        — even if two concurrent inserts raced, only one wins."""
        first = await circuit_breaker.record(db_session, CircuitBreakerTrigger.MANUAL.value)
        second = await circuit_breaker.record(db_session, CircuitBreakerTrigger.MANUAL.value)
        assert first is not None
        assert second is None
        # Verify only ONE row exists.
        rows = (await db_session.execute(select(CircuitBreaker))).scalars().all()
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# P1 #10 — circuit_breaker.find_active (new helper)
# ---------------------------------------------------------------------------


class TestFindActive:
    async def test_returns_active_row(self, db_session):
        row = await circuit_breaker.record(
            db_session,
            CircuitBreakerTrigger.MACRO_EVENT.value,
            details={"reason": "x"},
        )
        assert row is not None
        found = await circuit_breaker.find_active(
            db_session, CircuitBreakerTrigger.MACRO_EVENT.value
        )
        assert found is not None
        assert found.id == row.id
        assert found.details == {"reason": "x"}

    async def test_returns_none_when_resolved(self, db_session):
        await circuit_breaker.record(db_session, CircuitBreakerTrigger.MANUAL.value)
        await circuit_breaker.resolve(
            db_session, CircuitBreakerTrigger.MANUAL.value, resolved_by="ops"
        )
        found = await circuit_breaker.find_active(db_session, CircuitBreakerTrigger.MANUAL.value)
        assert found is None


# ---------------------------------------------------------------------------
# P1 #6 — submit_cancel in-flight idempotency
# ---------------------------------------------------------------------------


class TestCancelIdempotency:
    async def test_second_submit_with_signature_already_set_replays(self, db_session):
        """Order is ACKED with cancel-payload + cancel-signature already
        persisted (from a prior submit_cancel call). Second call MUST
        short-circuit with replayed=True and NOT call the gateway."""
        u = await _seed_user(db_session)
        # Order in ACKED with cancel-side fully set up.
        payload = '{"type":"cancelOrder","params":{}}'
        order = await _seed_order(
            db_session,
            user_id=u.id,
            status=OrderStatus.ACKED.value,
            eip712_payload='{"type":"newOrder","params":{}}',
            eip712_signature=_VALID_SIG,
            eip712_payload_hash="0x" + "cd" * 32,
            nonce=1700000000000,
            nonce_expires_at=datetime.now(UTC) + timedelta(hours=12),
            cancel_eip712_payload=payload,
            cancel_eip712_signature=_VALID_SIG,
            cancel_nonce=1700000000001,
        )

        spot = AsyncMock(spec=SodexSpotClient)
        spot.submit_batch_cancel_order = AsyncMock()
        perps = AsyncMock(spec=SodexPerpsClient)
        perps.submit_batch_cancel_order = AsyncMock()

        result = await submit_cancel(
            db_session,
            order_id=order.id,
            signature=_VALID_SIG,
            spot_client=spot,
            perps_client=perps,
        )
        assert result.replayed is True
        assert result.status == OrderStatus.ACKED.value
        # CRITICAL: gateway MUST NOT have been called.
        spot.submit_batch_cancel_order.assert_not_called()
        perps.submit_batch_cancel_order.assert_not_called()


# ---------------------------------------------------------------------------
# P0 #5 — SodexAuthError terminalizes (re-pin in this file too)
# ---------------------------------------------------------------------------


class TestAuthErrorTerminal:
    async def test_submit_new_auth_error_becomes_rejected(self, db_session):
        u = await _seed_user(db_session)
        payload = '{"type":"newOrder","params":{}}'
        order = await _seed_order(
            db_session,
            user_id=u.id,
            status=OrderStatus.PENDING.value,
            eip712_payload=payload,
            eip712_payload_hash="0x" + "cd" * 32,
            nonce=1700000000000,
            nonce_expires_at=datetime.now(UTC) + timedelta(hours=12),
        )

        spot = AsyncMock(spec=SodexSpotClient)
        spot.submit_batch_new_order = AsyncMock(
            side_effect=SodexAuthError("API key not found", code=-1, raw_error="API key not found")
        )
        perps = AsyncMock(spec=SodexPerpsClient)

        result = await submit_new(
            db_session,
            order_id=order.id,
            signature=_VALID_SIG,
            spot_client=spot,
            perps_client=perps,
        )
        assert result.status == OrderStatus.REJECTED.value
        assert "auth:" in (result.error_message or "")
        # Pin the underlying row too — make sure REJECTED is persisted.
        await db_session.refresh(order)
        assert order.status == OrderStatus.REJECTED.value


# Suppress unused-import warning for entities only used in fixtures.
_ = Position
_ = PositionSide
