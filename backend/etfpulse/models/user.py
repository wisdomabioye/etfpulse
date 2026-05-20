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
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from etfpulse.models.base import Base


class UserRole(StrEnum):
    USER = "user"
    ADMIN = "admin"


class ChannelType(StrEnum):
    TELEGRAM = "telegram"
    EMAIL = "email"
    DISCORD = "discord"


class DeliveryPrefsMixin:
    """Delivery-preference columns shared between `User` and `TelegramGroup`.

    Both surfaces are independent delivery targets — fan-out treats them
    symmetrically — so the prefs schema MUST stay byte-identical across the
    two tables. PR I.2's `delivery_min_confirmation` shipped User-only on
    its first revision and had to be back-mirrored to groups; this mixin
    closes that drift path.

    The mixin owns *columns only*. Each subclass declares its own indexes
    (delivery paths differ — user fan-out joins channels, group fan-out
    joins chat_id) and its own `CheckConstraint` for `pref_min_confidence`
    (constraint names must be unique per-table). Don't try to push
    `__table_args__` into the mixin — composing it with the subclass's
    own constraint tuple is messier than the one duplicate CHECK line.
    """

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    pref_assets: Mapped[list[str]] = mapped_column(
        ARRAY(String(10)), default=lambda: ["BTC", "ETH"], nullable=False
    )
    pref_min_confidence: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    pref_paused: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Non-critical preferences (UI settings, display prefs). JSONB has no
    # write-side validation today — typos in keys propagate silently.
    # Future: gate writes through a Pydantic schema at the API boundary.
    preferences: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)


class User(Base, DeliveryPrefsMixin):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    role: Mapped[str] = mapped_column(String(20), default=UserRole.USER, nullable=False)
    agreed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # SoDEX wallet binding (Stage 09). NULL = user has not connected a wallet
    # (Telegram-only users + dashboard-only users are valid). Stored
    # lowercased (CHECK below enforces) so case-insensitive equality is the
    # natural string equality. The trust binding (proof-of-ownership via
    # signed challenge) is enforced at the route boundary in PR D.4 — this
    # column is the persisted result, not the verification step.
    #
    # 1:1 user↔wallet by design (V1 scope: a user signs for their own
    # positions). If we ever support multi-wallet users (Ledger + hot
    # wallet, team multisig, etc.), the migration path is a separate
    # `user_wallets` junction table — DO NOT silently extend this column
    # into a JSONB array or comma-separated string.
    wallet_address: Mapped[str | None] = mapped_column(String(42), nullable=True)

    # PR D.3 — SoDEX execution-surface bindings.
    #
    # `sodex_account_id` is the gateway's numeric `accountID` (the `aid`
    # field on `GET /accounts/{addr}/state` responses, V.3-verified stable
    # per wallet). Cached so prepare_order doesn't have to round-trip the
    # gateway just to look it up. Populated at wallet-bind time (D.4).
    #
    # `sodex_{spot,perps}_api_key_name` is the NAME of the registered API
    # key per venue, used in the `X-API-Key` header on signed writes.
    # The gateway looks up the named key on the target accountID and
    # recovers the signer from `X-API-Sign`. Registration is per-venue
    # (V.3 finding) so we cache one name per venue, NULL = not yet
    # registered on that venue. D.4 owns the bind flow.
    #
    # `paper_trade` is an operator-set, per-user toggle: True means
    # `submit_order` short-circuits before the HTTP call and writes a
    # simulated ACK. Order.paper_trade is COPIED from this column at
    # `prepare_order` time (atomically under SELECT FOR UPDATE) so an
    # operator flip mid-stream doesn't reclassify in-flight orders.
    sodex_account_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sodex_spot_api_key_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sodex_perps_api_key_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    paper_trade: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default="false"
    )

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
        # 0x + 40 lowercase hex chars. Forces normalization at write-time so
        # every downstream comparison can use plain `==` without `.lower()`.
        CheckConstraint(
            "wallet_address IS NULL OR wallet_address ~ '^0x[0-9a-f]{40}$'",
            name="ck_users_wallet_address_format",
        ),
        Index("ix_users_delivery", "is_active", "pref_paused", "pref_min_confidence"),
        Index("ix_users_pref_assets", "pref_assets", postgresql_using="gin"),
        # Partial unique: one wallet per user, but many users may have NULL.
        # Drives wallet-based reverse lookup at the API boundary.
        Index(
            "ix_users_wallet_address",
            "wallet_address",
            unique=True,
            postgresql_where=text("wallet_address IS NOT NULL"),
        ),
    )

    def __repr__(self) -> str:
        return f"<User id={self.id} role={self.role} active={self.is_active}>"


class NotificationChannel(Base):
    __tablename__ = "notification_channels"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
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


class TelegramGroup(Base, DeliveryPrefsMixin):
    __tablename__ = "telegram_groups"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    added_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
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
        Index("ix_groups_delivery", "is_active", "pref_paused", "pref_min_confidence"),
        Index("ix_groups_pref_assets", "pref_assets", postgresql_using="gin"),
    )

    def __repr__(self) -> str:
        return f"<TelegramGroup id={self.id} chat={self.chat_id} active={self.is_active}>"
