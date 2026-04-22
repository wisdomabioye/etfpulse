from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from etfpulse.models.base import Base


class UserRole(StrEnum):
    USER = "user"
    ADMIN = "admin"


class UserTier(StrEnum):
    FREE = "free"
    PREMIUM = "premium"


class ChannelType(StrEnum):
    TELEGRAM = "telegram"
    EMAIL = "email"
    DISCORD = "discord"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    role: Mapped[str] = mapped_column(String(20), default=UserRole.USER, nullable=False)
    tier: Mapped[str] = mapped_column(String(20), default=UserTier.FREE, nullable=False)
    tier_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    agreed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Delivery preferences — indexed
    pref_assets: Mapped[list[str]] = mapped_column(
        ARRAY(String(10)), default=lambda: ["BTC", "ETH"], nullable=False
    )
    pref_min_confidence: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    pref_paused: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Non-critical preferences (UI settings, display prefs)
    preferences: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "pref_min_confidence BETWEEN 1 AND 10",
            name="ck_users_pref_min_confidence_range",
        ),
        Index("ix_users_delivery", "is_active", "pref_paused", "pref_min_confidence"),
        Index("ix_users_tier", "tier", "tier_expires_at"),
        Index("ix_users_pref_assets", "pref_assets", postgresql_using="gin"),
    )

    def __repr__(self) -> str:
        return f"<User id={self.id} role={self.role} tier={self.tier} active={self.is_active}>"


class NotificationChannel(Base):
    __tablename__ = "notification_channels"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    channel_type: Mapped[str] = mapped_column(String(20), nullable=False)
    channel_identifier: Mapped[str] = mapped_column(String(100), nullable=False)
    username: Mapped[str | None] = mapped_column(String(100), nullable=True)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    failure_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_delivery_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_channels_user_active", "user_id", "channel_type", "is_active"),
        Index("ix_channels_unique", "channel_type", "channel_identifier", unique=True),
        Index("ix_channels_lookup", "channel_type", "channel_identifier"),
    )

    def __repr__(self) -> str:
        return (
            f"<NotificationChannel id={self.id} user={self.user_id} "
            f"type={self.channel_type} active={self.is_active}>"
        )


class TelegramGroup(Base):
    __tablename__ = "telegram_groups"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    added_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    tier: Mapped[str] = mapped_column(String(20), default=UserTier.FREE, nullable=False)
    tier_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    pref_assets: Mapped[list[str]] = mapped_column(
        ARRAY(String(10)), default=lambda: ["BTC", "ETH"], nullable=False
    )
    pref_min_confidence: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    pref_paused: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Non-critical group preferences (display/UI — symmetric with User.preferences)
    preferences: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "pref_min_confidence BETWEEN 1 AND 10",
            name="ck_groups_pref_min_confidence_range",
        ),
        Index("ix_groups_delivery", "is_active", "tier", "pref_paused"),
        Index("ix_groups_pref_assets", "pref_assets", postgresql_using="gin"),
    )

    def __repr__(self) -> str:
        return (
            f"<TelegramGroup id={self.id} chat={self.chat_id} "
            f"tier={self.tier} active={self.is_active}>"
        )
