from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from etfpulse.models.base import Base


class SignalType(StrEnum):
    FLOW_ANOMALY = "flow_anomaly"
    MAGNITUDE = "magnitude"
    ACCELERATION = "acceleration"
    DIVERGENCE = "divergence"
    REGIME_SHIFT = "regime_shift"


class SignalStatus(StrEnum):
    PENDING = "pending"
    ALERTED = "alerted"
    EXPIRED = "expired"


class SignalDirection(StrEnum):
    LONG = "long"
    SHORT = "short"
    NEUTRAL = "neutral"


class Signal(Base):
    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    signal_type: Mapped[str] = mapped_column(String(30), nullable=False)
    asset: Mapped[str] = mapped_column(String(10), nullable=False)
    trigger_data: Mapped[dict] = mapped_column(JSONB, nullable=False)
    ai_analysis: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    confidence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default=SignalStatus.PENDING, nullable=False)
    price_at_creation: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    # Provenance tag for price_at_creation. NULL only when the price itself is NULL
    # (both providers failed). Stage 8 uses this to decide whether 24h/72h klines
    # come from the same source as entry — mixing providers risks micro-skew.
    price_source: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # Stage 08-P1 — AI-suggested entry/stop/target price levels mirrored from
    # `ai_analysis` JSONB onto dedicated columns for indexable queries
    # (track-record summaries, outcome evaluator) without parsing JSONB on
    # every read. NULL when the AI suggested "wait" OR when AI failed at
    # build time (see `pipeline.signal_builder`). Same pattern as
    # `confidence` above — JSONB is the audit trail, columns are the
    # canonical projection. Outcome evaluator (`pipeline.track_record`)
    # reads from these columns, never from JSONB.
    entry_price: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    stop_price: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    target_price: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    # Hardcoded "v1" prior to Stage 07; bumped only on prompt-structure changes
    # that affect AI calibration (not wording edits). Used by the track-record
    # query to compare apples-to-apples across prompt revisions (issue #32).
    ai_prompt_version: Mapped[str] = mapped_column(String(10), nullable=False, server_default="v1")
    # 32 hex chars = 128 bits; combined with signal_date, collisions are not a concern.
    fingerprint: Mapped[str] = mapped_column(String(32), nullable=False)
    signal_date: Mapped[date] = mapped_column(Date, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    alerted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "confidence IS NULL OR confidence BETWEEN 1 AND 10",
            name="ck_signals_confidence_range",
        ),
        CheckConstraint(
            "ai_prompt_version ~ '^v[0-9]+$'",
            name="ck_signals_ai_prompt_version_format",
        ),
        Index("ix_signals_fingerprint_date", "fingerprint", "signal_date", unique=True),
        Index("ix_signals_created", "created_at"),
        Index("ix_signals_asset_created", "asset", "created_at"),
        Index("ix_signals_expires", "expires_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<Signal id={self.id} type={self.signal_type} asset={self.asset} "
            f"confidence={self.confidence} status={self.status}>"
        )


class SignalOutcome(Base):
    """Frozen evaluation of a Signal.

    `asset`, `signal_type`, and `confidence` are denormalized from the parent Signal
    to keep the track-record query (ix_outcomes_track_record) single-table. They MUST
    be copied from the Signal at insert time and treated as immutable — never update
    them independently of the parent row.
    """

    __tablename__ = "signal_outcomes"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    signal_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("signals.id"), nullable=False, unique=True
    )
    asset: Mapped[str] = mapped_column(String(10), nullable=False)
    signal_type: Mapped[str] = mapped_column(String(30), nullable=False)
    direction: Mapped[str] = mapped_column(String(10), nullable=False)
    confidence: Mapped[int] = mapped_column(Integer, nullable=False)
    entry_price: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    stop_price: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    target_price: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    price_at_signal: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    price_after_24h: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    price_after_72h: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    hit_target: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    hit_stop: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    max_favorable: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    max_adverse: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "confidence BETWEEN 1 AND 10",
            name="ck_outcomes_confidence_range",
        ),
        Index("ix_outcomes_evaluated", "evaluated_at"),
        Index("ix_outcomes_track_record", "asset", "signal_type", "hit_target"),
    )

    def __repr__(self) -> str:
        return (
            f"<SignalOutcome id={self.id} signal={self.signal_id} "
            f"hit_target={self.hit_target} hit_stop={self.hit_stop}>"
        )
