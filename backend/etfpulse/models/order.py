from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from etfpulse.models.base import Base


class OrderStatus(StrEnum):
    """Lifecycle states for a SoDEX order, in chronological progression.

    PENDING — row inserted, EIP-712 typed-data built, awaiting client signature
              (wallet-side via wagmi/viem). Reaper auto-EXPIRES if nonce window
              passes without a signature.
    SUBMITTED — signature received, request POSTed to SoDEX gateway, awaiting
                ack. If the gateway returns a synchronous error, transitions
                straight to REJECTED.
    ACKED — gateway returned a 2xx with `exchange_order_id`; order is live on
            the SoDEX book. Awaiting fills via WebSocket / reconciliation.
    PARTIALLY_FILLED, FILLED — fill states reported by the venue.
    CANCELLED — user-initiated cancel succeeded (DELETE /trade/orders).
    REJECTED — gateway refused (validation, insufficient balance, etc.).
    EXPIRED — nonce window passed without ack, or order TTL hit via the
              user-supplied `expires_at` deadline. Terminal.

    Order matters: any state change MUST progress monotonically through this
    enum. The reaper + reconciliation paths assume terminal states
    (FILLED, CANCELLED, REJECTED, EXPIRED) are never re-opened.
    """

    PENDING = "pending"
    SUBMITTED = "submitted"
    ACKED = "acked"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"


# Terminal states — used by reaper guards + reconciliation to short-circuit.
TERMINAL_ORDER_STATUSES: frozenset[OrderStatus] = frozenset(
    {OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED, OrderStatus.EXPIRED}
)


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"


class TimeInForce(StrEnum):
    GTC = "gtc"
    IOC = "ioc"
    FOK = "fok"


class Venue(StrEnum):
    """Execution venue. Shared with Position — orders and positions live on the same venue.

    SoDEX is split into two distinct gateways with different EIP-712 domain
    names (`spot` / `futures`), different chainIds (286623 / 138565 testnet),
    and different request schemas. Treating them as one venue obscured this
    pre-Stage-09; the split was deferred from C.1 and lands here. The
    pre-existing literal `"sodex"` is REMOVED — no production rows
    referenced it (Stage 09 hasn't shipped).
    """

    SODEX_SPOT = "sodex_spot"
    SODEX_PERPS = "sodex_perps"


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    signal_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("signals.id", ondelete="SET NULL"), nullable=True
    )
    venue: Mapped[str] = mapped_column(String(20), nullable=False)
    asset: Mapped[str] = mapped_column(String(10), nullable=False)
    side: Mapped[str] = mapped_column(String(10), nullable=False)
    order_type: Mapped[str] = mapped_column(String(10), nullable=False)
    time_in_force: Mapped[str] = mapped_column(String(10), nullable=False)
    # Sizes use Numeric(28, 18) — generous scale covers wei-denominated quantities
    # on EVM venues without forcing tokens to round at submission. Prices /
    # fiat-denominated values use Numeric(18, 8) — 8 decimals matches Binance
    # / SoSoValue conventions.
    requested_size: Mapped[Decimal] = mapped_column(Numeric(28, 18), nullable=False)
    requested_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    filled_size: Mapped[Decimal | None] = mapped_column(Numeric(28, 18), nullable=True)
    filled_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    filled_value: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    fees: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default=OrderStatus.PENDING, nullable=False)
    exchange_order_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    client_order_id: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Paper-trade flag (#64): True = simulated, no on-chain submission. Defaults
    # False so any code path that omits the kwarg gets the safe real-execution
    # path's CHECK / circuit-breaker treatment. Operators flip to True per-user
    # via a future admin toggle; Stage 09 wires the executor to this flag.
    paper_trade: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default="false"
    )
    # Outbound request payload captured at submission time. Distinct from
    # raw_response (the venue's reply) so a debug session has the full
    # before/after pair. NULL on legacy rows + on paper trades where no
    # request is built.
    raw_request: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    raw_response: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        # Keep this literal list in sync with `OrderStatus`. Test
        # `test_order_status_enum.py` round-trips every value to catch drift.
        CheckConstraint(
            "status IN ('pending','submitted','acked','partially_filled',"
            "'filled','cancelled','rejected','expired')",
            name="ck_orders_status_enum",
        ),
        # Venue is restricted to the two SoDEX gateways. Mirrors `Venue` enum.
        CheckConstraint(
            "venue IN ('sodex_spot','sodex_perps')",
            name="ck_orders_venue_enum",
        ),
        Index("ix_orders_user", "user_id"),
        Index("ix_orders_status", "status"),
        Index("ix_orders_exchange", "exchange_order_id"),
        Index("ix_orders_signal", "signal_id"),
    )

    def __repr__(self) -> str:
        return (
            f"<Order id={self.id} asset={self.asset} side={self.side} "
            f"type={self.order_type} status={self.status}>"
        )
