"""Focused tests for D.3.2 review-driven fixes.

Pins:
  - symbols_refresh tolerates a malformed venue symbol (one bad row no
    longer aborts the whole-venue batch).
  - prepare_cancel clears prior cancel_signature/submitted/error so
    submit_cancel can re-attempt after gateway-side failure.
  - submit_new with NULL payload_hash returns REJECTED (no assertion
    blow-up under `python -O`).
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

from sqlalchemy import select

from etfpulse.adapters.sodex.perps_client import SodexPerpsClient
from etfpulse.adapters.sodex.responses import PerpsSymbol, SpotSymbol
from etfpulse.adapters.sodex.spot_client import SodexSpotClient
from etfpulse.models import (
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    SodexSymbol,
    TimeInForce,
    User,
    Venue,
)
from etfpulse.pipeline.execution.pipeline import prepare_cancel, submit_new
from etfpulse.pipeline.symbols_refresh import refresh_sodex_symbols

_VALID_SIG = "0x01" + "ab" * 65


# ---------------------------------------------------------------------------
# D.3.2 step 3 — per-row resilience in symbols_refresh
# ---------------------------------------------------------------------------


class TestSymbolsRefreshPerRowResilience:
    async def test_malformed_symbol_skipped_others_persist(self, db_session):
        """One bad venue row (extract_asset raises) does NOT abort the batch.
        Good rows still land + `spot_parse_errors` reports the count."""
        # 3 spot symbols: one valid, one malformed (_USDC → empty asset),
        # one valid. After D.3.2, summary should show 2 inserted + 1 parse_error.
        spot = AsyncMock(spec=SodexSpotClient)
        spot.get_symbols = AsyncMock(
            return_value=[
                SpotSymbol.model_construct(id=1, name="vBTC_vUSDC"),
                SpotSymbol.model_construct(id=99, name="_USDC"),  # malformed
                SpotSymbol.model_construct(id=2, name="vETH_vUSDC"),
            ]
        )
        perps = AsyncMock(spec=SodexPerpsClient)
        perps.get_symbols = AsyncMock(return_value=[])

        summary = await refresh_sodex_symbols(db_session, spot_client=spot, perps_client=perps)

        assert summary["spot_inserted"] == 2
        assert summary["spot_parse_errors"] == 1
        assert summary["errors"] == 0  # HTTP-level errors only

        # The two valid rows persisted.
        rows = (
            (await db_session.execute(select(SodexSymbol).order_by(SodexSymbol.symbol_id)))
            .scalars()
            .all()
        )
        assert {r.symbol_id for r in rows} == {1, 2}
        assert all(r.asset in {"BTC", "ETH"} for r in rows)

    async def test_perps_malformed_symbol_isolated_from_spot(self, db_session):
        """A bad perps row doesn't poison the spot side either."""
        spot = AsyncMock(spec=SodexSpotClient)
        spot.get_symbols = AsyncMock(
            return_value=[SpotSymbol.model_construct(id=1, name="vBTC_vUSDC")]
        )
        perps = AsyncMock(spec=SodexPerpsClient)
        perps.get_symbols = AsyncMock(
            return_value=[
                PerpsSymbol.model_construct(id=42, name="_USDC"),  # bad
                PerpsSymbol.model_construct(id=43, name="vETH_vUSDC"),  # good
            ]
        )

        summary = await refresh_sodex_symbols(db_session, spot_client=spot, perps_client=perps)
        assert summary["spot_inserted"] == 1
        assert summary["spot_parse_errors"] == 0
        assert summary["perps_inserted"] == 1
        assert summary["perps_parse_errors"] == 1


# ---------------------------------------------------------------------------
# D.3.2 step 4 — prepare_cancel clears prior signature on regeneration
# (enables retry after gateway-side cancel failure)
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


async def _seed_btc_spot_symbol(db_session) -> None:
    sym = SodexSymbol(
        venue=Venue.SODEX_SPOT.value,
        symbol_id=1,
        name="vBTC_vUSDC",
        asset="BTC",
        raw={"id": 1, "name": "vBTC_vUSDC"},
    )
    db_session.add(sym)
    await db_session.flush()


class TestPrepareCancelClearsStaleSignature:
    async def test_regenerated_cancel_resets_signature_and_error(self, db_session):
        """A previous submit_cancel failed at gateway, leaving cancel_error_message
        + cancel_signature populated. Calling prepare_cancel again MUST clear
        these so the next submit_cancel can re-attempt with the fresh nonce."""
        await _seed_btc_spot_symbol(db_session)
        u = await _seed_user(db_session)
        # Order ACKED with stale cancel-side state (simulating a failed cancel).
        order = Order(
            user_id=u.id,
            client_order_id=f"o-{secrets.token_hex(4)}",
            venue=Venue.SODEX_SPOT.value,
            asset="BTC",
            side=OrderSide.BUY.value,
            order_type=OrderType.LIMIT.value,
            time_in_force=TimeInForce.GTC.value,
            requested_size=Decimal("0.01"),
            requested_price=Decimal("65000"),
            status=OrderStatus.ACKED.value,
            exchange_order_id="99999",
            eip712_payload='{"type":"newOrder","params":{}}',
            eip712_payload_hash="0x" + "cd" * 32,
            eip712_signature=_VALID_SIG,
            nonce=1700000000000,
            nonce_expires_at=datetime.now(UTC) + timedelta(hours=12),
            symbol_id=1,
            account_id=u.sodex_account_id,
            cancel_eip712_payload='{"type":"cancelOrder","params":{}}',
            cancel_eip712_payload_hash="0x" + "ef" * 32,
            cancel_eip712_signature=_VALID_SIG,
            cancel_nonce=1700000000001,
            cancel_submitted_at=datetime.now(UTC) - timedelta(seconds=30),
            cancel_error_message="order rejected: OrderNotFound",
        )
        db_session.add(order)
        await db_session.flush()

        result = await prepare_cancel(db_session, user_id=u.id, order_id=order.id)
        assert result.allow is True

        # Reload + verify the stale cancel-side state has been cleared.
        await db_session.refresh(order)
        assert order.cancel_eip712_signature is None
        assert order.cancel_submitted_at is None
        assert order.cancel_error_message is None
        # Fresh payload + nonce installed.
        assert order.cancel_nonce is not None
        assert order.cancel_nonce != 1700000000001
        assert order.cancel_eip712_payload is not None
        assert order.cancel_eip712_payload_hash is not None


# ---------------------------------------------------------------------------
# D.3.2 step 4 — submit_new with NULL payload_hash returns REJECTED
# (no assertion blow-up under `python -O`)
# ---------------------------------------------------------------------------


class TestSubmitNewMissingHash:
    async def test_null_payload_hash_returns_rejected(self, db_session):
        """Defensive runtime check (was an `assert` pre-D.3.2). An order
        somehow lacking eip712_payload_hash at submit time must NOT raise
        AssertionError — return REJECTED with a clear error."""
        await _seed_btc_spot_symbol(db_session)
        u = await _seed_user(db_session)
        # Force-construct an Order with payload but NULL hash (would normally
        # be impossible — the prepare path sets both. This pins the runtime
        # guard against future drift / direct DB tampering).
        order = Order(
            user_id=u.id,
            client_order_id=f"o-{secrets.token_hex(4)}",
            venue=Venue.SODEX_SPOT.value,
            asset="BTC",
            side=OrderSide.BUY.value,
            order_type=OrderType.LIMIT.value,
            time_in_force=TimeInForce.GTC.value,
            requested_size=Decimal("0.01"),
            requested_price=Decimal("65000"),
            status=OrderStatus.PENDING.value,
            eip712_payload='{"type":"newOrder","params":{}}',
            eip712_payload_hash=None,  # ← the invariant violation
            nonce=1700000000000,
            nonce_expires_at=datetime.now(UTC) + timedelta(hours=12),
            symbol_id=1,
            account_id=u.sodex_account_id,
        )
        db_session.add(order)
        await db_session.flush()

        result = await submit_new(
            db_session,
            order_id=order.id,
            signature=_VALID_SIG,
            spot_client=AsyncMock(spec=SodexSpotClient),
            perps_client=AsyncMock(spec=SodexPerpsClient),
        )
        assert result.status == OrderStatus.REJECTED.value
        assert "missing_payload_hash" in (result.error_message or "")
