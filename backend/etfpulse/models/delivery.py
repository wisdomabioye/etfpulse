from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from etfpulse.models.base import Base


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    DELIVERED = "delivered"
    FAILED = "failed"
    SKIPPED = "skipped"


class SignalDelivery(Base):
    __tablename__ = "signal_deliveries"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    signal_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("signals.id"), nullable=False)
    # Exactly one of (user_id + channel_id) or (group_id alone) is populated.
    # DM delivery → user_id NOT NULL, channel_id NOT NULL, group_id NULL.
    # Group delivery → group_id NOT NULL, user_id NULL, channel_id NULL
    # (groups are addressed by the TelegramGroup's chat_id directly, not via a channel row).
    user_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=True)
    group_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("telegram_groups.id"), nullable=True
    )
    channel_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("notification_channels.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(20), default=DeliveryStatus.PENDING, nullable=False)
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "(user_id IS NOT NULL AND group_id IS NULL AND channel_id IS NOT NULL) OR "
            "(user_id IS NULL AND group_id IS NOT NULL AND channel_id IS NULL)",
            name="ck_delivery_target",
        ),
        # Idempotency: one row per (signal, recipient). Retries update the existing row
        # (status, attempt_count, error_message) rather than inserting a duplicate.
        Index(
            "ux_delivery_user_signal",
            "signal_id",
            "user_id",
            "channel_id",
            unique=True,
            postgresql_where=text("user_id IS NOT NULL"),
        ),
        Index(
            "ux_delivery_group_signal",
            "signal_id",
            "group_id",
            unique=True,
            postgresql_where=text("group_id IS NOT NULL"),
        ),
        Index("ix_delivery_signal", "signal_id"),
        Index("ix_delivery_user", "user_id", "created_at"),
        Index("ix_delivery_group", "group_id", "created_at"),
        Index("ix_delivery_pending", "status"),
        Index("ix_delivery_cleanup", "created_at"),
    )

    def __repr__(self) -> str:
        target = f"user={self.user_id}" if self.user_id else f"group={self.group_id}"
        return (
            f"<SignalDelivery id={self.id} signal={self.signal_id} {target} status={self.status}>"
        )
