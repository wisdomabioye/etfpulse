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
from sqlalchemy.orm import Mapped, mapped_column

from etfpulse.models.base import Base
from etfpulse.models.order import Venue  # shared enum — positions and orders use the same venue


class PositionStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    CANCELLED = "cancelled"


class PositionSide(StrEnum):
    LONG = "long"
    SHORT = "short"


__all__ = ["Position", "PositionStatus", "PositionSide", "Venue"]


class Position(Base):
    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    signal_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("signals.id", ondelete="SET NULL"), nullable=True
    )
    order_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("orders.id", ondelete="SET NULL"), nullable=True
    )
    venue: Mapped[str] = mapped_column(String(20), nullable=False)
    asset: Mapped[str] = mapped_column(String(10), nullable=False)
    side: Mapped[str] = mapped_column(String(10), nullable=False)
    # See Order.requested_size for the Numeric(28, 18) vs Numeric(18, 8) rationale.
    size: Mapped[Decimal] = mapped_column(Numeric(28, 18), nullable=False)
    entry_price: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    stop_loss: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    take_profit: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default=PositionStatus.OPEN, nullable=False)
    # Paper-trade flag (#64) — see Order.paper_trade for the contract. A position
    # inherits the flag from the order that opened it; the executor copies it
    # explicitly rather than dereferencing through Order to keep this row
    # readable when Order.user_id is SET NULL'd at user deletion.
    paper_trade: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default="false"
    )
    # PR D.3 — perps-only. NULL on spot positions (spot has no leverage
    # concept). When set, carries the leverage the opening order requested;
    # the executor copies it from the Order row at fill time. Numeric(8,2)
    # admits values up to 999999.99 — well beyond any sane venue cap, but
    # cheap to allocate.
    leverage: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), nullable=True)
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    close_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    realized_pnl: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('open','closed','cancelled')",
            name="ck_positions_status_enum",
        ),
        # Mirror of Order.venue CHECK — see Venue enum docstring.
        CheckConstraint(
            "venue IN ('sodex_spot','sodex_perps')",
            name="ck_positions_venue_enum",
        ),
        # PR D.3 — leverage must be positive when set. NULL admitted
        # (spot positions never carry leverage).
        CheckConstraint(
            "leverage IS NULL OR leverage > 0",
            name="ck_positions_leverage_positive",
        ),
        Index("ix_positions_user", "user_id"),
        Index("ix_positions_status", "status"),
        Index("ix_positions_signal", "signal_id"),
        Index("ix_positions_order", "order_id"),
    )

    def __repr__(self) -> str:
        return (
            f"<Position id={self.id} asset={self.asset} side={self.side} "
            f"status={self.status} venue={self.venue}>"
        )
