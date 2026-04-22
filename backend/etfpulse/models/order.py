from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Numeric, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from etfpulse.models.base import Base


class OrderStatus(StrEnum):
    PENDING = "pending"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


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
    """Execution venue. Shared with Position — orders and positions live on the same venue."""

    SODEX = "sodex"


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=True)
    signal_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("signals.id"), nullable=True
    )
    venue: Mapped[str] = mapped_column(String(20), nullable=False)
    asset: Mapped[str] = mapped_column(String(10), nullable=False)
    side: Mapped[str] = mapped_column(String(10), nullable=False)
    order_type: Mapped[str] = mapped_column(String(10), nullable=False)
    time_in_force: Mapped[str] = mapped_column(String(10), nullable=False)
    requested_size: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    requested_price: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    filled_size: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    filled_price: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    filled_value: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    fees: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default=OrderStatus.PENDING, nullable=False)
    exchange_order_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    client_order_id: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    raw_response: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
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
