"""Orchestrator tests — `prepare_new` / `submit_new` / `prepare_cancel`
/ `submit_cancel` end-to-end.

Test surfaces (load-bearing for D.3):

  - `TestPrepareNew` — happy path, risk deny passthrough, symbol-cache
    miss, paper-trade flag copies to Order, signal_id passthrough.
  - `TestSubmitNew` — paper fill end-to-end, real envelope rejection,
    auth-error leaves SUBMITTED, idempotent replay, signature
    validation, nonce expiry guard.
  - `TestPrepareCancel` — PENDING local cancel, SUBMITTED race block,
    ACKED real-cancel typed-data, terminal no-op.
  - `TestSubmitCancel` — paper cancel, real-cancel OrderNotFound,
    real-cancel success, idempotent terminal.
  - `TestBytePreservation` — Order.eip712_payload bytes are byte-exact
    with bundle.payload_json (anti-drift rule 31).

Mock clients: subclasses of `SodexSpotClient`/`SodexPerpsClient` that
record args + return canned `OrderResponseItem` lists. They never make
HTTP — `submit_new` only calls one method per submit.
"""

from __future__ import annotations

import dataclasses
import json
import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock

import pytest

from etfpulse.adapters.sodex import (
    OrderSide as SodexOrderSide,
)
from etfpulse.adapters.sodex import (
    OrderType as SodexOrderType,
)
from etfpulse.adapters.sodex import (
    PositionSide,
    SodexAuthError,
    SodexEnvelopeError,
    SodexPerpsClient,
    SodexSpotClient,
)
from etfpulse.adapters.sodex import (
    StopType as SodexStopType,
)
from etfpulse.adapters.sodex import (
    TimeInForce as SodexTimeInForce,
)
from etfpulse.adapters.sodex import (
    TriggerType as SodexTriggerType,
)
from etfpulse.adapters.sodex.responses import OrderResponseItem
from etfpulse.models import (
    Order,
    OrderStatus,
    Position,
    SodexSymbol,
    User,
    Venue,
)
from etfpulse.pipeline.execution.pipeline import (
    prepare_cancel,
    prepare_new,
    submit_cancel,
    submit_new,
)
from etfpulse.pipeline.execution.risk import RiskRequest


def _wallet() -> str:
    return "0x" + secrets.token_hex(20)


async def _seed_user(db_session, *, paper_trade: bool = False) -> int:
    u = User(
        wallet_address=_wallet(),
        sodex_account_id=57436,
        sodex_spot_api_key_name="default",
        sodex_perps_api_key_name="default",
        paper_trade=paper_trade,
    )
    db_session.add(u)
    await db_session.flush()
    return u.id


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


async def _seed_btc_perps_symbol(db_session) -> None:
    sym = SodexSymbol(
        venue=Venue.SODEX_PERPS.value,
        symbol_id=2,
        name="vBTC_vUSDC",
        asset="BTC",
        raw={"id": 2, "name": "vBTC_vUSDC"},
    )
    db_session.add(sym)
    await db_session.flush()


def _spot_request(
    *, asset: str = "BTC", price: Decimal = Decimal("65000"), size: Decimal = Decimal("0.01")
) -> RiskRequest:
    return RiskRequest(
        venue=Venue.SODEX_SPOT.value,
        asset=asset,
        side=SodexOrderSide.BUY.value,
        order_type=SodexOrderType.LIMIT.value,
        time_in_force=SodexTimeInForce.GTC.value,
        requested_size=size,
        requested_price=price,
    )


def _perps_request() -> RiskRequest:
    return RiskRequest(
        venue=Venue.SODEX_PERPS.value,
        asset="BTC",
        side=SodexOrderSide.BUY.value,
        order_type=SodexOrderType.LIMIT.value,
        time_in_force=SodexTimeInForce.GTC.value,
        requested_size=Decimal("0.01"),
        requested_price=Decimal("65000"),
        position_side=PositionSide.BOTH.value,
        leverage=Decimal("3"),
    )


# ---------------------------------------------------------------------------
# Mock clients — record + return canned items
# ---------------------------------------------------------------------------


def _make_mock_spot_client(*, response_items: list[OrderResponseItem]) -> Any:
    client = AsyncMock(spec=SodexSpotClient)
    client.submit_batch_new_order = AsyncMock(return_value=response_items)
    client.submit_batch_cancel_order = AsyncMock(return_value=response_items)
    return client


def _make_mock_perps_client(*, response_items: list[OrderResponseItem]) -> Any:
    client = AsyncMock(spec=SodexPerpsClient)
    client.submit_batch_new_order = AsyncMock(return_value=response_items)
    client.submit_batch_cancel_order = AsyncMock(return_value=response_items)
    return client


# ---------------------------------------------------------------------------
# prepare_new
# ---------------------------------------------------------------------------


class TestPrepareNew:
    async def test_happy_path_spot(self, db_session):
        await _seed_btc_spot_symbol(db_session)
        uid = await _seed_user(db_session)

        result = await prepare_new(db_session, user_id=uid, request=_spot_request())

        assert result.allow is True
        assert result.order_id is not None
        assert result.typed_data is not None
        assert result.client_order_id is not None
        assert result.client_order_id.startswith("ep-s-")
        assert result.nonce is not None

        # Order persisted with PENDING status, byte-exact payload.
        from sqlalchemy import select

        order = (
            await db_session.execute(select(Order).where(Order.id == result.order_id))
        ).scalar_one()
        assert order.status == OrderStatus.PENDING.value
        assert order.venue == Venue.SODEX_SPOT.value
        assert order.eip712_payload is not None
        assert order.eip712_payload_hash is not None
        assert order.eip712_payload_hash.startswith("0x")
        assert len(order.eip712_payload_hash) == 66
        assert order.nonce == result.nonce
        assert order.nonce_expires_at is not None
        # Reasonable nonce-window expiry — within 25h.
        assert order.nonce_expires_at - datetime.now(UTC) > timedelta(hours=23)
        assert order.client_order_id == result.client_order_id

    async def test_happy_path_perps(self, db_session):
        await _seed_btc_perps_symbol(db_session)
        uid = await _seed_user(db_session)

        result = await prepare_new(db_session, user_id=uid, request=_perps_request())
        assert result.allow is True
        assert result.client_order_id.startswith("ep-p-")

    async def test_risk_deny_passes_through(self, db_session):
        """User has no wallet — risk denies. prepare_new must NOT INSERT."""
        await _seed_btc_spot_symbol(db_session)
        u = User(
            sodex_account_id=57436,
            sodex_spot_api_key_name="default",
        )
        db_session.add(u)
        await db_session.flush()

        result = await prepare_new(db_session, user_id=u.id, request=_spot_request())
        assert result.allow is False
        assert result.reason == "wallet_not_bound"
        assert result.order_id is None

        # No Order row created.
        from sqlalchemy import select

        rows = (
            (await db_session.execute(select(Order).where(Order.user_id == u.id))).scalars().all()
        )
        assert rows == []

    async def test_symbol_cache_miss_raises(self, db_session):
        """Symbol not in cache → SymbolNotResolved. Caller (route)
        translates to 503."""
        from etfpulse.pipeline.execution.symbols import SymbolNotResolved

        uid = await _seed_user(db_session)
        # No symbol seeded — cache is empty.
        with pytest.raises(SymbolNotResolved):
            await prepare_new(db_session, user_id=uid, request=_spot_request())

    async def test_paper_flag_copies_to_order(self, db_session):
        await _seed_btc_spot_symbol(db_session)
        uid = await _seed_user(db_session, paper_trade=True)

        result = await prepare_new(db_session, user_id=uid, request=_spot_request())
        from sqlalchemy import select

        order = (
            await db_session.execute(select(Order).where(Order.id == result.order_id))
        ).scalar_one()
        assert order.paper_trade is True

    async def test_signal_id_passthrough(self, db_session):
        """If the route passes signal_id, it carries onto Order."""
        await _seed_btc_spot_symbol(db_session)
        uid = await _seed_user(db_session)

        # Seed a Signal so the FK validates.
        from etfpulse.models import Signal

        signal = Signal(
            signal_type="flow_anomaly",
            asset="BTC",
            signal_date=datetime.now(UTC).date(),
            fingerprint="deadbeef" * 4,
            trigger_data={},
        )
        db_session.add(signal)
        await db_session.flush()

        result = await prepare_new(
            db_session,
            user_id=uid,
            request=_spot_request(),
            signal_id=signal.id,
        )
        from sqlalchemy import select

        order = (
            await db_session.execute(select(Order).where(Order.id == result.order_id))
        ).scalar_one()
        assert order.signal_id == signal.id


# ---------------------------------------------------------------------------
# PR P1-fix.CRIT-1 — stop fields reach the SIGNED payload
# ---------------------------------------------------------------------------


def _perps_stop_request(
    *,
    stop_type: str = "stop_loss",
    side: int = SodexOrderSide.SELL.value,
    reduce_only: bool = True,
) -> RiskRequest:
    """A perps protective leg: MARKET reduce-only stop with a mark-price
    trigger — the exact shape the FE order chain produces for SL/TP."""
    return RiskRequest(
        venue=Venue.SODEX_PERPS.value,
        asset="BTC",
        side=side,
        order_type=SodexOrderType.MARKET.value,
        time_in_force=SodexTimeInForce.IOC.value,
        requested_size=Decimal("0.01"),
        requested_price=Decimal("65000"),
        position_side=PositionSide.BOTH.value,
        trigger_type=SodexTriggerType.MARK_PRICE.value,
        leverage=Decimal("3"),
        is_conditional=True,
        stop_price=Decimal("60000"),
        stop_type=stop_type,
        reduce_only=reduce_only,
    )


class TestStopFieldsReachSignedPayload:
    """Regression for PR P1-fix.CRIT-1. Pre-fix, `_build_new_order_bundle`
    hardcoded `stopPrice=None`, `stopType=None`, `reduceOnly=False`, so a
    'stop-loss' was signed as a plain immediate market order. These tests
    parse the byte-exact `Order.eip712_payload` (what the wallet signs +
    the gateway re-hashes) and assert the stop semantics are actually
    present."""

    async def _prepared_order(self, db_session, request: RiskRequest) -> Order:
        from sqlalchemy import select

        await _seed_btc_perps_symbol(db_session)
        uid = await _seed_user(db_session)
        result = await prepare_new(db_session, user_id=uid, request=request)
        return (
            await db_session.execute(select(Order).where(Order.id == result.order_id))
        ).scalar_one()

    async def test_stop_loss_payload_carries_stop_fields(self, db_session):
        order = await self._prepared_order(db_session, _perps_stop_request())
        # DB row is correct (was already true pre-fix)...
        assert order.stop_price == Decimal("60000")
        assert order.stop_type == "stop_loss"
        assert order.reduce_only is True
        # ...and now the SIGNED payload carries them too.
        payload = json.loads(order.eip712_payload)
        item = payload["params"]["orders"][0]
        assert item["stopPrice"] == "60000"
        assert item["stopType"] == SodexStopType.STOP_LOSS.value  # 1
        assert item["triggerType"] == SodexTriggerType.MARK_PRICE.value  # 2
        assert item["reduceOnly"] is True

    async def test_take_profit_maps_to_wire_int_two(self, db_session):
        order = await self._prepared_order(db_session, _perps_stop_request(stop_type="take_profit"))
        item = json.loads(order.eip712_payload)["params"]["orders"][0]
        assert item["stopType"] == SodexStopType.TAKE_PROFIT.value  # 2

    async def test_non_stop_perps_order_omits_stop_price(self, db_session):
        """A plain perps order (no stop) must NOT carry stopPrice in the
        payload — serialization excludes None, so the key is absent."""
        order = await self._prepared_order(db_session, _perps_request())
        item = json.loads(order.eip712_payload)["params"]["orders"][0]
        assert "stopPrice" not in item
        assert item["reduceOnly"] is False

    async def test_reduce_only_close_payload_carries_reduce_only(self, db_session):
        """PR P1-fix.TEST-GAP — a market reduce-only CLOSE (no stop) must
        sign `reduceOnly:true` into the payload, else the gateway could
        oversell past zero into opposite exposure. This is the exact
        row-vs-payload divergence CRIT-1 was; pin it at the payload byte
        level, not just on the DB row."""
        close_req = RiskRequest(
            venue=Venue.SODEX_PERPS.value,
            asset="BTC",
            side=SodexOrderSide.SELL.value,
            order_type=SodexOrderType.MARKET.value,
            time_in_force=SodexTimeInForce.IOC.value,
            requested_size=Decimal("0.01"),
            requested_price=Decimal("65000"),
            position_side=PositionSide.BOTH.value,
            leverage=Decimal("3"),
            reduce_only=True,
            is_conditional=False,
        )
        order = await self._prepared_order(db_session, close_req)
        item = json.loads(order.eip712_payload)["params"]["orders"][0]
        assert item["reduceOnly"] is True
        assert "stopPrice" not in item  # a close is not a stop order


# ---------------------------------------------------------------------------
# submit_new — paper path
# ---------------------------------------------------------------------------


class TestSubmitNewPaper:
    async def test_paper_full_fill_creates_position(self, db_session):
        """Paper trade short-circuits to FILLED + opens spot Position."""
        await _seed_btc_spot_symbol(db_session)
        uid = await _seed_user(db_session, paper_trade=True)

        prepared = await prepare_new(db_session, user_id=uid, request=_spot_request())
        # Compute the signature from the bundle hash. For test
        # purposes any well-formed signature passes the regex.
        sig = "0x01" + "ab" * 65

        result = await submit_new(
            db_session,
            order_id=prepared.order_id,
            signature=sig,
            spot_client=_make_mock_spot_client(response_items=[]),
            perps_client=_make_mock_perps_client(response_items=[]),
        )
        assert result.status == OrderStatus.FILLED.value
        assert result.exchange_order_id == f"PAPER-{prepared.order_id}"
        assert result.replayed is False

        # Position opened with paper_trade=True.
        from sqlalchemy import select

        position = (
            await db_session.execute(select(Position).where(Position.user_id == uid))
        ).scalar_one()
        assert position.paper_trade is True
        assert position.size == Decimal("0.01")
        # 5 bps slippage → 65000 * 1.0005 = 65032.5.
        assert position.entry_price == Decimal("65032.50000000")

    async def test_paper_perps_short_path(self, db_session):
        """Perps + SELL + paper → opens SHORT Position."""
        await _seed_btc_perps_symbol(db_session)
        uid = await _seed_user(db_session, paper_trade=True)

        req = dataclasses.replace(_perps_request(), side=SodexOrderSide.SELL.value)
        prepared = await prepare_new(db_session, user_id=uid, request=req)
        sig = "0x01" + "ab" * 65

        result = await submit_new(
            db_session,
            order_id=prepared.order_id,
            signature=sig,
            spot_client=_make_mock_spot_client(response_items=[]),
            perps_client=_make_mock_perps_client(response_items=[]),
        )
        assert result.status == OrderStatus.FILLED.value
        from sqlalchemy import select

        position = (
            await db_session.execute(select(Position).where(Position.user_id == uid))
        ).scalar_one()
        from etfpulse.models.position import PositionSide as DbPositionSide

        assert position.side == DbPositionSide.SHORT.value

    async def test_paper_reduce_only_no_position_rejected(self, db_session):
        """PR P1-fix.RO-FILL-1 — a paper reduce_only order with no position
        to reduce must be REJECTED (mirroring the gateway), NOT recorded as
        a phantom FILLED that opens exposure. Guards the CAP-EXEMPT
        invariant: reduce_only can't create exposure even in paper."""
        from sqlalchemy import select

        await _seed_btc_perps_symbol(db_session)
        uid = await _seed_user(db_session, paper_trade=True)
        # A non-conditional reduce_only SELL with no open position.
        req = _perps_stop_request(side=SodexOrderSide.SELL.value)
        req = dataclasses.replace(req, stop_price=None, stop_type=None, is_conditional=False)
        prepared = await prepare_new(db_session, user_id=uid, request=req)
        result = await submit_new(
            db_session,
            order_id=prepared.order_id,
            signature="0x01" + "ab" * 65,
            spot_client=_make_mock_spot_client(response_items=[]),
            perps_client=_make_mock_perps_client(response_items=[]),
        )
        assert result.status == OrderStatus.REJECTED.value
        order = (
            await db_session.execute(select(Order).where(Order.id == prepared.order_id))
        ).scalar_one()
        assert order.filled_size is None
        # No position opened by the reduce_only no-op.
        positions = (
            (await db_session.execute(select(Position).where(Position.user_id == uid)))
            .scalars()
            .all()
        )
        assert positions == []

    async def test_paper_conditional_rests_acked_no_position(self, db_session):
        """PR P1-fix.PAPER-1 — a paper conditional (stop) order must NOT
        instant-fill: it rests ACKED with no Position change, so attaching
        a stop doesn't close the position. Mirrors the real-venue
        trigger-pending state."""
        from sqlalchemy import select

        await _seed_btc_perps_symbol(db_session)
        uid = await _seed_user(db_session, paper_trade=True)

        prepared = await prepare_new(db_session, user_id=uid, request=_perps_stop_request())
        result = await submit_new(
            db_session,
            order_id=prepared.order_id,
            signature="0x01" + "ab" * 65,
            spot_client=_make_mock_spot_client(response_items=[]),
            perps_client=_make_mock_perps_client(response_items=[]),
        )
        # ACKED (trigger pending), NOT filled.
        assert result.status == OrderStatus.ACKED.value
        assert result.exchange_order_id == f"PAPER-{prepared.order_id}"

        order = (
            await db_session.execute(select(Order).where(Order.id == prepared.order_id))
        ).scalar_one()
        assert order.is_conditional is True
        assert order.filled_size is None  # never filled

        # No Position created — the stop hasn't triggered.
        positions = (
            (await db_session.execute(select(Position).where(Position.user_id == uid)))
            .scalars()
            .all()
        )
        assert positions == []


# ---------------------------------------------------------------------------
# submit_new — real path with mock clients
# ---------------------------------------------------------------------------


class TestSubmitNewReal:
    async def _setup_pending_order(self, db_session) -> int:
        await _seed_btc_spot_symbol(db_session)
        uid = await _seed_user(db_session)
        prepared = await prepare_new(db_session, user_id=uid, request=_spot_request())
        return prepared.order_id

    async def test_ack_response_transitions_to_acked(self, db_session):
        order_id = await self._setup_pending_order(db_session)
        spot = _make_mock_spot_client(
            response_items=[OrderResponseItem(code=0, clOrdID="ep-s-x", orderID=99999)]
        )
        result = await submit_new(
            db_session,
            order_id=order_id,
            signature="0x01" + "ab" * 65,
            spot_client=spot,
            perps_client=_make_mock_perps_client(response_items=[]),
        )
        assert result.status == OrderStatus.ACKED.value
        assert result.exchange_order_id == "99999"
        spot.submit_batch_new_order.assert_called_once()

    async def test_per_order_rejection_transitions_to_rejected(self, db_session):
        order_id = await self._setup_pending_order(db_session)
        spot = _make_mock_spot_client(
            response_items=[
                OrderResponseItem(code=-1, clOrdID="ep-s-x", error="insufficient margin")
            ]
        )
        result = await submit_new(
            db_session,
            order_id=order_id,
            signature="0x01" + "ab" * 65,
            spot_client=spot,
            perps_client=_make_mock_perps_client(response_items=[]),
        )
        assert result.status == OrderStatus.REJECTED.value
        assert "insufficient margin" in (result.error_message or "")

    async def test_envelope_error_transitions_to_rejected(self, db_session):
        order_id = await self._setup_pending_order(db_session)
        spot = AsyncMock(spec=SodexSpotClient)
        spot.submit_batch_new_order = AsyncMock(
            side_effect=SodexEnvelopeError(
                "signature recovery failed", code=-1, raw_error="signature recovery failed"
            )
        )
        result = await submit_new(
            db_session,
            order_id=order_id,
            signature="0x01" + "ab" * 65,
            spot_client=spot,
            perps_client=_make_mock_perps_client(response_items=[]),
        )
        assert result.status == OrderStatus.REJECTED.value
        assert "envelope:" in (result.error_message or "")

    async def test_auth_error_terminalizes_as_rejected(self, db_session):
        """PR D.3.1: auth errors are deterministic (replay-protected by
        gateway nonce-tracking), NOT transient. submit_new transitions
        to REJECTED so the order exits the active set + the user can
        re-prepare with a fresh nonce after fixing the api-key binding."""
        order_id = await self._setup_pending_order(db_session)
        spot = AsyncMock(spec=SodexSpotClient)
        spot.submit_batch_new_order = AsyncMock(
            side_effect=SodexAuthError("API key not found", code=-1, raw_error="API key not found")
        )
        result = await submit_new(
            db_session,
            order_id=order_id,
            signature="0x01" + "ab" * 65,
            spot_client=spot,
            perps_client=_make_mock_perps_client(response_items=[]),
        )
        assert result.status == OrderStatus.REJECTED.value
        assert "auth:" in (result.error_message or "")


# ---------------------------------------------------------------------------
# submit_new — idempotency + validation
# ---------------------------------------------------------------------------


class TestSubmitNewGuards:
    async def test_idempotent_replay(self, db_session):
        """Second submit_new on the same already-ACKED order returns
        `replayed=True` and DOES NOT call the gateway again."""
        await _seed_btc_spot_symbol(db_session)
        uid = await _seed_user(db_session)
        prepared = await prepare_new(db_session, user_id=uid, request=_spot_request())

        spot = _make_mock_spot_client(
            response_items=[OrderResponseItem(code=0, clOrdID="x", orderID=99)]
        )
        # First submit — ACKED.
        await submit_new(
            db_session,
            order_id=prepared.order_id,
            signature="0x01" + "ab" * 65,
            spot_client=spot,
            perps_client=_make_mock_perps_client(response_items=[]),
        )

        # Second submit — must NOT call spot again.
        spot.submit_batch_new_order.reset_mock()
        result2 = await submit_new(
            db_session,
            order_id=prepared.order_id,
            signature="0x01" + "ab" * 65,
            spot_client=spot,
            perps_client=_make_mock_perps_client(response_items=[]),
        )
        assert result2.replayed is True
        assert result2.status == OrderStatus.ACKED.value
        spot.submit_batch_new_order.assert_not_called()

    async def test_malformed_signature_rejected(self, db_session):
        await _seed_btc_spot_symbol(db_session)
        uid = await _seed_user(db_session)
        prepared = await prepare_new(db_session, user_id=uid, request=_spot_request())

        result = await submit_new(
            db_session,
            order_id=prepared.order_id,
            signature="not-a-valid-signature",
            spot_client=_make_mock_spot_client(response_items=[]),
            perps_client=_make_mock_perps_client(response_items=[]),
        )
        assert result.status == OrderStatus.REJECTED.value
        assert result.error_message == "invalid_signature_format"

    async def test_nonce_expired_rejected(self, db_session):
        await _seed_btc_spot_symbol(db_session)
        uid = await _seed_user(db_session)
        prepared = await prepare_new(db_session, user_id=uid, request=_spot_request())
        # Forcibly expire the nonce window.
        from sqlalchemy import select

        order = (
            await db_session.execute(select(Order).where(Order.id == prepared.order_id))
        ).scalar_one()
        order.nonce_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await db_session.flush()

        result = await submit_new(
            db_session,
            order_id=prepared.order_id,
            signature="0x01" + "ab" * 65,
            spot_client=_make_mock_spot_client(response_items=[]),
            perps_client=_make_mock_perps_client(response_items=[]),
        )
        assert result.status == OrderStatus.REJECTED.value
        assert result.error_message == "nonce_window_expired"


# ---------------------------------------------------------------------------
# prepare_cancel
# ---------------------------------------------------------------------------


class TestPrepareCancel:
    async def test_pending_local_cancel(self, db_session):
        """PENDING → flip to CANCELLED locally; no venue contact."""
        await _seed_btc_spot_symbol(db_session)
        uid = await _seed_user(db_session)
        prepared = await prepare_new(db_session, user_id=uid, request=_spot_request())

        result = await prepare_cancel(db_session, user_id=uid, order_id=prepared.order_id)
        assert result.allow is True
        assert result.local_only is True
        assert result.typed_data is None

        from sqlalchemy import select

        order = (
            await db_session.execute(select(Order).where(Order.id == prepared.order_id))
        ).scalar_one()
        assert order.status == OrderStatus.CANCELLED.value
        # No cancel_eip712_payload (no signature needed).
        assert order.cancel_eip712_payload is None

    async def test_submitted_blocked(self, db_session):
        """SUBMITTED — race window. Cancel denied."""
        await _seed_btc_spot_symbol(db_session)
        uid = await _seed_user(db_session)
        prepared = await prepare_new(db_session, user_id=uid, request=_spot_request())
        # Force into SUBMITTED state.
        from sqlalchemy import select

        order = (
            await db_session.execute(select(Order).where(Order.id == prepared.order_id))
        ).scalar_one()
        order.status = OrderStatus.SUBMITTED.value
        order.eip712_signature = "0x01" + "ab" * 65
        await db_session.flush()

        result = await prepare_cancel(db_session, user_id=uid, order_id=prepared.order_id)
        assert result.allow is False
        assert result.reason == "cancel_blocked_in_flight"

    async def test_acked_builds_cancel_typed_data(self, db_session):
        await _seed_btc_spot_symbol(db_session)
        uid = await _seed_user(db_session)
        prepared = await prepare_new(db_session, user_id=uid, request=_spot_request())
        from sqlalchemy import select

        order = (
            await db_session.execute(select(Order).where(Order.id == prepared.order_id))
        ).scalar_one()
        order.status = OrderStatus.ACKED.value
        order.eip712_signature = "0x01" + "ab" * 65
        order.exchange_order_id = "12345"
        await db_session.flush()

        result = await prepare_cancel(db_session, user_id=uid, order_id=prepared.order_id)
        assert result.allow is True
        assert result.local_only is False
        assert result.typed_data is not None
        assert result.nonce is not None
        assert result.nonce != prepared.nonce  # fresh nonce for cancel

        # Cancel payload persisted on the Order row.
        await db_session.refresh(order)
        assert order.cancel_eip712_payload is not None
        assert order.cancel_eip712_payload_hash is not None
        assert order.cancel_nonce == result.nonce

    async def test_terminal_idempotent(self, db_session):
        await _seed_btc_spot_symbol(db_session)
        uid = await _seed_user(db_session)
        prepared = await prepare_new(db_session, user_id=uid, request=_spot_request())
        from sqlalchemy import select

        order = (
            await db_session.execute(select(Order).where(Order.id == prepared.order_id))
        ).scalar_one()
        order.status = OrderStatus.FILLED.value
        await db_session.flush()

        result = await prepare_cancel(db_session, user_id=uid, order_id=prepared.order_id)
        assert result.allow is True
        assert result.replayed is True

    async def test_wrong_user_unauthorized(self, db_session):
        await _seed_btc_spot_symbol(db_session)
        uid_owner = await _seed_user(db_session)
        uid_other = await _seed_user(db_session)
        prepared = await prepare_new(db_session, user_id=uid_owner, request=_spot_request())
        result = await prepare_cancel(db_session, user_id=uid_other, order_id=prepared.order_id)
        assert result.allow is False
        assert result.reason == "unauthorized"


# ---------------------------------------------------------------------------
# submit_cancel
# ---------------------------------------------------------------------------


class TestSubmitCancel:
    async def _setup_acked_order_with_prepared_cancel(self, db_session) -> int:
        await _seed_btc_spot_symbol(db_session)
        uid = await _seed_user(db_session)
        prepared = await prepare_new(db_session, user_id=uid, request=_spot_request())
        from sqlalchemy import select

        order = (
            await db_session.execute(select(Order).where(Order.id == prepared.order_id))
        ).scalar_one()
        order.status = OrderStatus.ACKED.value
        order.eip712_signature = "0x01" + "ab" * 65
        order.exchange_order_id = "12345"
        await db_session.flush()
        await prepare_cancel(db_session, user_id=uid, order_id=order.id)
        return order.id

    async def test_cancel_accepted_transitions_to_cancelled(self, db_session):
        order_id = await self._setup_acked_order_with_prepared_cancel(db_session)
        spot = _make_mock_spot_client(response_items=[OrderResponseItem(code=0, clOrdID="x")])
        result = await submit_cancel(
            db_session,
            order_id=order_id,
            signature="0x01" + "cd" * 65,
            spot_client=spot,
            perps_client=_make_mock_perps_client(response_items=[]),
        )
        assert result.status == OrderStatus.CANCELLED.value
        spot.submit_batch_cancel_order.assert_called_once()

    async def test_order_not_found_leaves_status_with_error(self, db_session):
        """Cancel rejected (race with fill) — status unchanged, cancel_error_message
        recorded."""
        order_id = await self._setup_acked_order_with_prepared_cancel(db_session)
        spot = _make_mock_spot_client(
            response_items=[
                OrderResponseItem(code=-1, clOrdID=None, error="order rejected: OrderNotFound")
            ]
        )
        result = await submit_cancel(
            db_session,
            order_id=order_id,
            signature="0x01" + "cd" * 65,
            spot_client=spot,
            perps_client=_make_mock_perps_client(response_items=[]),
        )
        # Order is still ACKED (cancel didn't land).
        assert result.status == OrderStatus.ACKED.value
        assert "OrderNotFound" in (result.error_message or "")

    async def test_paper_cancel_transitions_local_only(self, db_session):
        await _seed_btc_spot_symbol(db_session)
        uid = await _seed_user(db_session, paper_trade=True)
        prepared = await prepare_new(db_session, user_id=uid, request=_spot_request())
        # Move to ACKED state without using real submit (mock the
        # paper fill skipping).
        from sqlalchemy import select

        order = (
            await db_session.execute(select(Order).where(Order.id == prepared.order_id))
        ).scalar_one()
        order.status = OrderStatus.ACKED.value
        order.eip712_signature = "0x01" + "ab" * 65
        order.exchange_order_id = "PAPER-1"
        await db_session.flush()
        await prepare_cancel(db_session, user_id=uid, order_id=order.id)

        spot = _make_mock_spot_client(response_items=[])
        result = await submit_cancel(
            db_session,
            order_id=order.id,
            signature="0x01" + "cd" * 65,
            spot_client=spot,
            perps_client=_make_mock_perps_client(response_items=[]),
        )
        assert result.status == OrderStatus.CANCELLED.value
        # Paper path MUST NOT call the gateway.
        spot.submit_batch_cancel_order.assert_not_called()

    async def test_terminal_replay(self, db_session):
        order_id = await self._setup_acked_order_with_prepared_cancel(db_session)
        # Force the order to already-CANCELLED.
        from sqlalchemy import select

        order = (await db_session.execute(select(Order).where(Order.id == order_id))).scalar_one()
        order.status = OrderStatus.CANCELLED.value
        await db_session.flush()

        result = await submit_cancel(
            db_session,
            order_id=order_id,
            signature="0x01" + "cd" * 65,
            spot_client=_make_mock_spot_client(response_items=[]),
            perps_client=_make_mock_perps_client(response_items=[]),
        )
        assert result.replayed is True
        assert result.status == OrderStatus.CANCELLED.value


# ---------------------------------------------------------------------------
# Byte preservation — anti-drift rule 31
# ---------------------------------------------------------------------------


class TestBytePreservation:
    async def test_payload_bytes_round_trip_via_extract(self, db_session):
        """Order.eip712_payload (TEXT) survives the round-trip exactly —
        the bytes the wallet would sign equal `bundle.payload_json`."""
        await _seed_btc_spot_symbol(db_session)
        uid = await _seed_user(db_session)
        prepared = await prepare_new(db_session, user_id=uid, request=_spot_request())

        from sqlalchemy import select

        order = (
            await db_session.execute(select(Order).where(Order.id == prepared.order_id))
        ).scalar_one()

        # The payload starts with the action wrapper, ends with `}`,
        # contains the structural marker exactly once.
        payload = order.eip712_payload
        assert payload is not None
        assert payload.startswith('{"type":"newOrder","params":')
        assert payload.endswith("}")
        assert payload.count(',"params":') == 1

        # `extract_params_bytes` succeeds on the stored TEXT.
        from etfpulse.pipeline.execution.bytes_helpers import extract_params_bytes

        body = extract_params_bytes(payload)
        assert body.startswith(b'{"accountID":57436')

    async def test_payload_hash_matches_recomputed(self, db_session):
        """eip712_payload_hash matches keccak256(eip712_payload bytes)."""
        from eth_utils import keccak

        await _seed_btc_spot_symbol(db_session)
        uid = await _seed_user(db_session)
        prepared = await prepare_new(db_session, user_id=uid, request=_spot_request())

        from sqlalchemy import select

        order = (
            await db_session.execute(select(Order).where(Order.id == prepared.order_id))
        ).scalar_one()
        recomputed = "0x" + keccak(text=order.eip712_payload).hex()
        assert order.eip712_payload_hash == recomputed
