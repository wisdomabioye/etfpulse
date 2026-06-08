"""Pin every CHECK constraint added to `orders` and `positions` by PR C.6.

Three classes of invariants:

1. **Enum membership** (`ck_orders_status_enum`, `ck_orders_venue_enum`,
   `ck_positions_venue_enum`). The 8 OrderStatus values, the 2 Venue
   values. Any drift between the Python enum and the DB CHECK clauses is
   caught here.
2. **EIP-712 integrity** (`ck_orders_nonce_consistency`,
   `ck_orders_signature_requires_payload`, `ck_orders_signature_format`).
   These are the load-bearing safety nets that prevent malformed Stage 09
   rows from being persisted — e.g., a signature without its payload, a
   nonce without its expiry, a signature that won't verify because of
   case-normalisation drift.

Every CHECK has both a positive case (a row that should be accepted) and
a negative case (a row that the constraint must reject).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from etfpulse.models import (
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    PositionSide,
    PositionStatus,
    StopType,
    TimeInForce,
    Venue,
)

# PR D.3 changed `eip712_payload` from JSONB to TEXT — the column now
# stores the byte-exact JSON the wallet signed, NOT a Python dict. Tests
# that exercise CHECK constraints pass this compact-JSON string so
# nothing about the byte-exactness contract leaks into test fixtures.
_PAYLOAD_TEXT = '{"type":"newOrder","params":{}}'


def _base_order_kwargs(
    *,
    client_order_id: str = "test-co-1",
    status: str = OrderStatus.PENDING.value,
    venue: str = Venue.SODEX_SPOT.value,
) -> dict:
    """Minimum-valid Order kwargs. Tests override individual fields."""
    return {
        "client_order_id": client_order_id,
        "venue": venue,
        "asset": "BTC",
        "side": OrderSide.BUY.value,
        "order_type": OrderType.LIMIT.value,
        "time_in_force": TimeInForce.GTC.value,
        "requested_size": Decimal("0.01"),
        "status": status,
    }


# ---------------------------------------------------------------------------
# Enum-membership CHECKs
# ---------------------------------------------------------------------------


class TestOrderStatusEnum:
    """Every value in `OrderStatus` MUST be accepted by `ck_orders_status_enum`.
    A 9th invented value MUST be rejected. Catches model↔CHECK drift."""

    @pytest.mark.parametrize("status", [s.value for s in OrderStatus])
    async def test_every_enum_value_accepted(self, db_session, status):
        kw = _base_order_kwargs(client_order_id=f"st-{status}", status=status)
        order = Order(**kw)
        db_session.add(order)
        await db_session.flush()
        assert order.status == status

    async def test_unknown_status_rejected(self, db_session):
        kw = _base_order_kwargs(client_order_id="st-bad", status="invented")
        order = Order(**kw)
        db_session.add(order)
        with pytest.raises(IntegrityError):
            await db_session.flush()
        await db_session.rollback()


class TestOrderVenueEnum:
    """`ck_orders_venue_enum` accepts only sodex_spot | sodex_perps."""

    @pytest.mark.parametrize("venue", [v.value for v in Venue])
    async def test_every_enum_value_accepted(self, db_session, venue):
        kw = _base_order_kwargs(client_order_id=f"v-{venue}", venue=venue)
        order = Order(**kw)
        db_session.add(order)
        await db_session.flush()
        assert order.venue == venue

    @pytest.mark.parametrize("bad_venue", ["sodex", "binance", "uniswap", "spot", "perps", ""])
    async def test_invalid_venue_rejected(self, db_session, bad_venue):
        kw = _base_order_kwargs(client_order_id=f"v-bad-{bad_venue}", venue=bad_venue)
        order = Order(**kw)
        db_session.add(order)
        with pytest.raises(IntegrityError):
            await db_session.flush()
        await db_session.rollback()


class TestPositionVenueEnum:
    """Mirror of Order venue CHECK on the Position table."""

    async def test_valid_venue_accepted(self, db_session):
        position = Position(
            venue=Venue.SODEX_PERPS.value,
            asset="BTC",
            side=PositionSide.LONG.value,
            size=Decimal("0.01"),
            entry_price=Decimal("65000"),
            status=PositionStatus.OPEN.value,
        )
        db_session.add(position)
        await db_session.flush()
        assert position.venue == Venue.SODEX_PERPS.value

    async def test_invalid_venue_rejected(self, db_session):
        position = Position(
            venue="not_a_venue",
            asset="BTC",
            side=PositionSide.LONG.value,
            size=Decimal("0.01"),
            entry_price=Decimal("65000"),
            status=PositionStatus.OPEN.value,
        )
        db_session.add(position)
        with pytest.raises(IntegrityError):
            await db_session.flush()
        await db_session.rollback()


# ---------------------------------------------------------------------------
# PR D.3 — positions.leverage CHECK (perps-only; NULL on spot)
# ---------------------------------------------------------------------------


class TestPositionLeverageCheck:
    """`leverage IS NULL OR leverage > 0` — perps leverage must be positive
    when set. NULL admitted (spot positions never carry leverage)."""

    async def test_null_leverage_accepted(self, db_session):
        position = Position(
            venue=Venue.SODEX_SPOT.value,
            asset="BTC",
            side=PositionSide.LONG.value,
            size=Decimal("0.01"),
            entry_price=Decimal("65000"),
            status=PositionStatus.OPEN.value,
        )
        db_session.add(position)
        await db_session.flush()
        assert position.leverage is None

    async def test_positive_leverage_accepted(self, db_session):
        position = Position(
            venue=Venue.SODEX_PERPS.value,
            asset="BTC",
            side=PositionSide.LONG.value,
            size=Decimal("0.01"),
            entry_price=Decimal("65000"),
            status=PositionStatus.OPEN.value,
            leverage=Decimal("3"),
        )
        db_session.add(position)
        await db_session.flush()
        assert position.leverage == Decimal("3")

    @pytest.mark.parametrize("bad_leverage", [Decimal("0"), Decimal("-1")])
    async def test_zero_or_negative_leverage_rejected(self, db_session, bad_leverage):
        position = Position(
            venue=Venue.SODEX_PERPS.value,
            asset="BTC",
            side=PositionSide.LONG.value,
            size=Decimal("0.01"),
            entry_price=Decimal("65000"),
            status=PositionStatus.OPEN.value,
            leverage=bad_leverage,
        )
        db_session.add(position)
        with pytest.raises(IntegrityError):
            await db_session.flush()
        await db_session.rollback()


# ---------------------------------------------------------------------------
# EIP-712 integrity CHECKs
# ---------------------------------------------------------------------------


class TestNonceConsistency:
    """`(nonce IS NULL) = (nonce_expires_at IS NULL)` — derived together
    at build time; one without the other is a code bug."""

    async def test_both_null_accepted(self, db_session):
        """Pre-Stage-09 / paper-trade rows: both NULL is the default."""
        order = Order(**_base_order_kwargs(client_order_id="nc-1"))
        db_session.add(order)
        await db_session.flush()

    async def test_both_set_accepted(self, db_session):
        nonce_ms = 1700000000000
        expiry = datetime.now(UTC) + timedelta(days=1)
        order = Order(
            **_base_order_kwargs(client_order_id="nc-2"),
            nonce=nonce_ms,
            nonce_expires_at=expiry,
        )
        db_session.add(order)
        await db_session.flush()

    async def test_nonce_without_expiry_rejected(self, db_session):
        order = Order(**_base_order_kwargs(client_order_id="nc-3"), nonce=42)
        db_session.add(order)
        with pytest.raises(IntegrityError):
            await db_session.flush()
        await db_session.rollback()

    async def test_expiry_without_nonce_rejected(self, db_session):
        order = Order(
            **_base_order_kwargs(client_order_id="nc-4"),
            nonce_expires_at=datetime.now(UTC),
        )
        db_session.add(order)
        with pytest.raises(IntegrityError):
            await db_session.flush()
        await db_session.rollback()


class TestSignatureRequiresPayload:
    """`signature IS NULL OR payload IS NOT NULL` — a signature with no
    payload would be unverifiable. Closes a malformed-write path."""

    async def test_signature_with_payload_accepted(self, db_session):
        sig = "0x01" + "ab" * 65
        order = Order(
            **_base_order_kwargs(client_order_id="sp-1"),
            eip712_payload=_PAYLOAD_TEXT,
            eip712_signature=sig,
        )
        db_session.add(order)
        await db_session.flush()

    async def test_payload_without_signature_accepted(self, db_session):
        """Build-time state: payload is written before client signs."""
        order = Order(
            **_base_order_kwargs(client_order_id="sp-2"),
            eip712_payload=_PAYLOAD_TEXT,
        )
        db_session.add(order)
        await db_session.flush()

    async def test_signature_without_payload_rejected(self, db_session):
        sig = "0x01" + "cd" * 65
        order = Order(
            **_base_order_kwargs(client_order_id="sp-3"),
            eip712_signature=sig,
        )
        db_session.add(order)
        with pytest.raises(IntegrityError):
            await db_session.flush()
        await db_session.rollback()


class TestSignatureFormat:
    """`signature ~ '^0x01[0-9a-f]{130}$'` — SoDEX type byte + 65-byte
    ECDSA signature in lowercase hex. Lowercased so the gateway's
    normalisation can't cause a replay mismatch."""

    async def test_valid_signature_accepted(self, db_session):
        sig = "0x01" + "ab" * 65
        order = Order(
            **_base_order_kwargs(client_order_id="sf-1"),
            eip712_payload=_PAYLOAD_TEXT,
            eip712_signature=sig,
        )
        db_session.add(order)
        await db_session.flush()
        assert order.eip712_signature == sig

    @pytest.mark.parametrize(
        "bad_sig",
        [
            "0x02" + "ab" * 65,  # wrong type byte (must be 0x01)
            "0x01" + "AB" * 65,  # uppercase hex rejected
            "0x01" + "ab" * 64,  # too short by one byte
            "0x01" + "ab" * 66,  # too long by one byte
            "0x" + "ab" * 65,  # missing type byte
            "01" + "ab" * 65,  # missing 0x prefix
            "0X01" + "ab" * 65,  # uppercase 0X prefix
            "0x01" + "gg" * 65,  # non-hex chars
        ],
    )
    async def test_malformed_signature_rejected(self, db_session, bad_sig):
        order = Order(
            **_base_order_kwargs(client_order_id=f"sf-bad-{hash(bad_sig)}"),
            eip712_payload=_PAYLOAD_TEXT,
            eip712_signature=bad_sig,
        )
        db_session.add(order)
        with pytest.raises(IntegrityError):
            await db_session.flush()
        await db_session.rollback()


# ---------------------------------------------------------------------------
# PR D.3 — payload_hash format CHECK
# ---------------------------------------------------------------------------


class TestEip712PayloadHashFormat:
    """`eip712_payload_hash ~ '^0x[0-9a-f]{64}$'` — keccak256 of payload."""

    async def test_valid_hash_accepted(self, db_session):
        order = Order(
            **_base_order_kwargs(client_order_id="ph-1"),
            eip712_payload=_PAYLOAD_TEXT,
            eip712_payload_hash="0x" + "ab" * 32,
        )
        db_session.add(order)
        await db_session.flush()

    @pytest.mark.parametrize(
        "bad_hash",
        [
            "0x" + "AB" * 32,  # uppercase
            "0x" + "ab" * 31,  # too short (62 hex chars)
            "ab" * 32,  # missing 0x prefix
            "0x" + "gg" * 32,  # non-hex chars
            # NB: "too long" cases are caught by VARCHAR(66) length
            # truncation (DataError), not by this CHECK regex. Tested
            # implicitly by the column type; not duplicated here.
        ],
    )
    async def test_malformed_hash_rejected(self, db_session, bad_hash):
        order = Order(
            **_base_order_kwargs(client_order_id=f"ph-bad-{hash(bad_hash)}"),
            eip712_payload=_PAYLOAD_TEXT,
            eip712_payload_hash=bad_hash,
        )
        db_session.add(order)
        with pytest.raises(IntegrityError):
            await db_session.flush()
        await db_session.rollback()


# ---------------------------------------------------------------------------
# PR D.3 — cancel-lifecycle invariants (mirror of new-order)
# ---------------------------------------------------------------------------


class TestCancelLifecycleInvariants:
    """`cancel_*` columns mirror the new-order EIP-712 invariants. CHECK
    constraints encode: nonce ⇔ payload (both set or both NULL);
    signature ⇒ payload; signature format ^0x01[0-9a-f]{130}$;
    payload_hash format ^0x[0-9a-f]{64}$.
    """

    async def test_build_time_state_accepted(self, db_session):
        """payload + nonce set, signature not yet — pre-submission state."""
        order = Order(
            **_base_order_kwargs(client_order_id="cl-1"),
            cancel_nonce=1700000000000,
            cancel_eip712_payload=_PAYLOAD_TEXT,
        )
        db_session.add(order)
        await db_session.flush()

    async def test_full_cancel_state_accepted(self, db_session):
        sig = "0x01" + "ab" * 65
        order = Order(
            **_base_order_kwargs(client_order_id="cl-2"),
            cancel_nonce=1700000000001,
            cancel_eip712_payload=_PAYLOAD_TEXT,
            cancel_eip712_signature=sig,
            cancel_eip712_payload_hash="0x" + "cd" * 32,
        )
        db_session.add(order)
        await db_session.flush()

    async def test_signature_without_payload_rejected(self, db_session):
        sig = "0x01" + "ab" * 65
        order = Order(
            **_base_order_kwargs(client_order_id="cl-3"),
            cancel_eip712_signature=sig,
        )
        db_session.add(order)
        with pytest.raises(IntegrityError):
            await db_session.flush()
        await db_session.rollback()

    async def test_nonce_without_payload_rejected(self, db_session):
        order = Order(
            **_base_order_kwargs(client_order_id="cl-4"),
            cancel_nonce=1700000000002,
        )
        db_session.add(order)
        with pytest.raises(IntegrityError):
            await db_session.flush()
        await db_session.rollback()

    async def test_payload_without_nonce_rejected(self, db_session):
        order = Order(
            **_base_order_kwargs(client_order_id="cl-5"),
            cancel_eip712_payload=_PAYLOAD_TEXT,
        )
        db_session.add(order)
        with pytest.raises(IntegrityError):
            await db_session.flush()
        await db_session.rollback()

    @pytest.mark.parametrize(
        "bad_sig",
        [
            "0x02" + "ab" * 65,  # wrong type byte
            "0x01" + "AB" * 65,  # uppercase hex
            "0x01" + "ab" * 64,  # short
        ],
    )
    async def test_malformed_signature_rejected(self, db_session, bad_sig):
        order = Order(
            **_base_order_kwargs(client_order_id=f"cl-bs-{hash(bad_sig)}"),
            cancel_nonce=1700000000003,
            cancel_eip712_payload=_PAYLOAD_TEXT,
            cancel_eip712_signature=bad_sig,
        )
        db_session.add(order)
        with pytest.raises(IntegrityError):
            await db_session.flush()
        await db_session.rollback()


# ---------------------------------------------------------------------------
# PR P1.1 — stop_price / stop_type / parent_order_id CHECKs
# ---------------------------------------------------------------------------


class TestStopTypeEnum:
    """Every value in `StopType` MUST be accepted by `ck_orders_stop_type_enum`.
    A bogus literal MUST be rejected. Catches model↔CHECK drift on the new
    stop-attachment column added in PR P1.1."""

    @pytest.mark.parametrize("stop_type", [s.value for s in StopType])
    async def test_every_enum_value_accepted(self, db_session, stop_type):
        kw = _base_order_kwargs(client_order_id=f"sp-{stop_type}")
        order = Order(**kw, stop_price=Decimal("100"), stop_type=stop_type)
        db_session.add(order)
        await db_session.flush()
        assert order.stop_type == stop_type

    async def test_unknown_stop_type_rejected(self, db_session):
        kw = _base_order_kwargs(client_order_id="sp-bad")
        order = Order(**kw, stop_price=Decimal("100"), stop_type="trailing_stop")
        db_session.add(order)
        with pytest.raises(IntegrityError):
            await db_session.flush()
        await db_session.rollback()


class TestStopPricePositive:
    """`ck_orders_stop_price_positive` — stop_price must be > 0 when set."""

    async def test_positive_accepted(self, db_session):
        kw = _base_order_kwargs(client_order_id="spp-1")
        order = Order(**kw, stop_price=Decimal("0.00000001"), stop_type=StopType.STOP_LOSS.value)
        db_session.add(order)
        await db_session.flush()

    @pytest.mark.parametrize("bad", [Decimal("0"), Decimal("-1")])
    async def test_non_positive_rejected(self, db_session, bad):
        kw = _base_order_kwargs(client_order_id=f"spp-{bad}")
        order = Order(**kw, stop_price=bad, stop_type=StopType.STOP_LOSS.value)
        db_session.add(order)
        with pytest.raises(IntegrityError):
            await db_session.flush()
        await db_session.rollback()


class TestStopPriceTypeConsistency:
    """`ck_orders_stop_price_type_consistency` — both NULL or both set."""

    async def test_both_null_accepted(self, db_session):
        kw = _base_order_kwargs(client_order_id="spt-null")
        order = Order(**kw)
        db_session.add(order)
        await db_session.flush()
        assert order.stop_price is None and order.stop_type is None

    async def test_both_set_accepted(self, db_session):
        kw = _base_order_kwargs(client_order_id="spt-both")
        order = Order(**kw, stop_price=Decimal("100"), stop_type=StopType.TAKE_PROFIT.value)
        db_session.add(order)
        await db_session.flush()

    async def test_price_without_type_rejected(self, db_session):
        kw = _base_order_kwargs(client_order_id="spt-p-only")
        order = Order(**kw, stop_price=Decimal("100"))
        db_session.add(order)
        with pytest.raises(IntegrityError):
            await db_session.flush()
        await db_session.rollback()

    async def test_type_without_price_rejected(self, db_session):
        kw = _base_order_kwargs(client_order_id="spt-t-only")
        order = Order(**kw, stop_type=StopType.STOP_LOSS.value)
        db_session.add(order)
        with pytest.raises(IntegrityError):
            await db_session.flush()
        await db_session.rollback()


class TestParentNotSelf:
    """`ck_orders_parent_not_self` — parent_order_id MUST NOT equal id."""

    async def test_null_parent_accepted(self, db_session):
        kw = _base_order_kwargs(client_order_id="pn-null")
        order = Order(**kw)
        db_session.add(order)
        await db_session.flush()
        assert order.parent_order_id is None

    async def test_distinct_parent_accepted(self, db_session):
        parent = Order(**_base_order_kwargs(client_order_id="pn-parent"))
        db_session.add(parent)
        await db_session.flush()
        child = Order(
            **_base_order_kwargs(client_order_id="pn-child"),
            parent_order_id=parent.id,
        )
        db_session.add(child)
        await db_session.flush()
        assert child.parent_order_id == parent.id

    async def test_self_parent_rejected(self, db_session):
        order = Order(**_base_order_kwargs(client_order_id="pn-self"))
        db_session.add(order)
        await db_session.flush()
        order.parent_order_id = order.id
        with pytest.raises(IntegrityError):
            await db_session.flush()
        await db_session.rollback()
