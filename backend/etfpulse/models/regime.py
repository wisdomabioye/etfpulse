from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from etfpulse.models.base import Base

# JSONB wrapper key inside `regime_snapshots.macro_events`. The column is
# typed `dict | None` (per the SQLAlchemy mapping below), so a list of
# event labels is stored as `{<key>: [...]}`. Pinned here — alongside the
# column — so every reader and writer (signal_builder writing, /api/regime
# reading, future Telegram formatter reading) agrees on the shape without
# importing a constant from the orchestrator.
REGIME_MACRO_EVENTS_KEY = "events_nearby"


class CircuitBreakerTrigger(StrEnum):
    MACRO_EVENT = "macro_event"  # high-impact macro event near — see Stage 09
    MANUAL = "manual"  # admin-initiated halt


class MarketRegime(StrEnum):
    """Wyckoff-inspired phases. Values are CHECK-constrained on regime_snapshots.regime."""

    ACCUMULATION = "accumulation"
    MARKUP = "markup"
    DISTRIBUTION = "distribution"
    MARKDOWN = "markdown"
    UNCERTAIN = "uncertain"


class SignalPosture(StrEnum):
    """How aggressively the regime monitor wants the signal pipeline to fire.

    AGGRESSIVE — clear regime, fire on lower-confidence detector hits.
    NORMAL     — default cadence.
    CAUTIOUS   — uncertain regime, raise the bar.
    PAUSED     — circuit-breaker active (macro event nearby); skip emission.
    """

    AGGRESSIVE = "aggressive"
    NORMAL = "normal"
    CAUTIOUS = "cautious"
    PAUSED = "paused"


class RegimeSnapshot(Base):
    __tablename__ = "regime_snapshots"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # btc_dominance is a percentage (~40-65 today); flow_trend_7d is a signed
    # ratio change. Numeric(8, 4) gives 9999.9999 max which is far beyond what
    # either field ever reaches but matches the FRACTION precision used in
    # signal_outcomes.max_favorable / max_adverse for consistency.
    btc_dominance: Mapped[Decimal | None] = mapped_column(Numeric(8, 4), nullable=True)
    flow_trend_7d: Mapped[Decimal | None] = mapped_column(Numeric(8, 4), nullable=True)
    macro_events: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    news_velocity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ai_interpretation: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Stage 07 — regime classifier output. Columns are nullable at the DB level
    # because (a) no rows exist yet — `ingest_regime_snapshot` still raises
    # NotImplementedError until the classifier lands in #100, and (b) once it
    # does, transient classifier failures should still produce a snapshot row
    # rather than aborting the daily cycle. Constrained values: see
    # MarketRegime / SignalPosture StrEnums above and the matching CHECK
    # constraints below.
    regime: Mapped[str | None] = mapped_column(String(30), nullable=True)
    signal_posture: Mapped[str | None] = mapped_column(String(20), nullable=True)
    confidence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Structured score breakdown (flow_score, dominance_score, news_score, …) so
    # the dashboard can render *why* without re-running the classifier. Schema
    # is open intentionally — the classifier owns it; consumers must tolerate
    # missing keys.
    reasoning: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "regime IS NULL OR regime IN "
            "('accumulation','markup','distribution','markdown','uncertain')",
            name="ck_regime_snapshots_regime_enum",
        ),
        CheckConstraint(
            "signal_posture IS NULL OR signal_posture IN "
            "('aggressive','normal','cautious','paused')",
            name="ck_regime_snapshots_posture_enum",
        ),
        CheckConstraint(
            "confidence IS NULL OR confidence BETWEEN 1 AND 10",
            name="ck_regime_snapshots_confidence_range",
        ),
        Index("ix_regime_captured", "captured_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<RegimeSnapshot id={self.id} date={self.captured_at} dominance={self.btc_dominance}>"
        )


class CircuitBreaker(Base):
    __tablename__ = "circuit_breakers"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    trigger_type: Mapped[str] = mapped_column(String(50), nullable=False)
    details: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    triggered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String(50), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "trigger_type IN ('macro_event','manual')",
            name="ck_circuit_breakers_trigger_type_enum",
        ),
    )

    def __repr__(self) -> str:
        status = "active" if self.resolved_at is None else "resolved"
        return f"<CircuitBreaker id={self.id} type={self.trigger_type} {status}>"
