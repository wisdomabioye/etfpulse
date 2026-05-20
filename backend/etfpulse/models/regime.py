from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
    text,
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
    """Reasons a circuit breaker may be tripped.

    A breaker carries an optional `user_id` (PR D.3) — NULL means the
    breaker is GLOBAL (halts every user's execution); non-NULL scopes
    the halt to one user. Global+per-user are independent dimensions:
    a global `manual` breaker halts everyone regardless of per-user
    state; a per-user `daily_loss_limit` halts only that user.
    """

    MACRO_EVENT = "macro_event"  # high-impact macro event near — see Stage 09
    MANUAL = "manual"  # admin-initiated halt (may be global or per-user)
    DAILY_LOSS_LIMIT = "daily_loss_limit"  # per-user; PR D.3 — D.4 wires the trip
    # Adding a new value? Update `ck_circuit_breakers_trigger_type_enum` CHECK
    # below + the matching CHECK in the migration that introduces the value.


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
    # PR D.3 — scope dimension. NULL = global (halts all users). Non-NULL =
    # halts only that user. ON DELETE SET NULL so deleting a user doesn't
    # cascade-delete audit history; the row stays as historical record with
    # an orphaned user_id reference.
    #
    # **Operational caveat** (PR D.3.1 documentation): deleting a user with
    # an UNRESOLVED per-user breaker row will SET NULL on that row → the
    # breaker silently converts to global scope (halts ALL users). The
    # partial unique index `uq_circuit_breakers_active_scope` also collapses
    # COALESCE(user_id, -1), so the now-global row could conflict with an
    # already-active global breaker. Rare in practice (user deletion is
    # uncommon, and ops should explicitly resolve per-user breakers before
    # deleting), but flagged here so the surprise is documented. Operators
    # can audit via /admin/metrics → `active_circuit_breakers > 0` should
    # always have an explanation.
    #
    # TODO(stage-10): a BEFORE DELETE trigger on `users` that resolves
    # any unresolved per-user breakers first would eliminate the
    # constraint-violation risk (FK ON DELETE cascade would no-op).
    # Deferred from D.3.2 because V1 doesn't delete users — the failure
    # mode is theoretical until operator tooling supports user deletion.
    user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    trigger_type: Mapped[str] = mapped_column(String(50), nullable=False)
    details: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    triggered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String(50), nullable=True)

    __table_args__ = (
        # Keep the literal list in sync with `CircuitBreakerTrigger` StrEnum.
        # PR D.3 added `daily_loss_limit` for per-user trips.
        CheckConstraint(
            "trigger_type IN ('macro_event','manual','daily_loss_limit')",
            name="ck_circuit_breakers_trigger_type_enum",
        ),
        # Partial index — risk-controller scan path. Two reads per
        # prepare_order (one global + one per-user) both filter on
        # `resolved_at IS NULL`; this index serves both via the (user_id)
        # prefix. Resolved rows are not indexed — they're audit-only.
        Index(
            "ix_circuit_breakers_user_unresolved",
            "user_id",
            "trigger_type",
            postgresql_where=text("resolved_at IS NULL"),
        ),
        # PR D.3.1 — partial UNIQUE on `(trigger_type, COALESCE(user_id,-1))
        # WHERE resolved_at IS NULL`. Enforces "one active breaker per
        # scope" at the DB layer so the SELECT-then-INSERT race in
        # `circuit_breaker.record()` is collapsed to a single-statement
        # `INSERT … ON CONFLICT DO NOTHING`. `-1` is a safe sentinel
        # for NULL user_id (global scope) because `users.id` is
        # BIGSERIAL starting at 1. Migration 8c61b9480195 creates this.
        Index(
            "uq_circuit_breakers_active_scope",
            "trigger_type",
            text("COALESCE(user_id, -1)"),
            unique=True,
            postgresql_where=text("resolved_at IS NULL"),
        ),
    )

    def __repr__(self) -> str:
        status = "active" if self.resolved_at is None else "resolved"
        scope = "global" if self.user_id is None else f"user={self.user_id}"
        return f"<CircuitBreaker id={self.id} type={self.trigger_type} {scope} {status}>"
