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
    TimeInForce,
    Venue,
)


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
            eip712_payload={"type": "newOrder", "params": {}},
            eip712_signature=sig,
        )
        db_session.add(order)
        await db_session.flush()

    async def test_payload_without_signature_accepted(self, db_session):
        """Build-time state: payload is written before client signs."""
        order = Order(
            **_base_order_kwargs(client_order_id="sp-2"),
            eip712_payload={"type": "newOrder", "params": {}},
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
            eip712_payload={"type": "newOrder", "params": {}},
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
            eip712_payload={"type": "newOrder", "params": {}},
            eip712_signature=bad_sig,
        )
        db_session.add(order)
        with pytest.raises(IntegrityError):
            await db_session.flush()
        await db_session.rollback()
