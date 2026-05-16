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
    SmallInteger,
    String,
    func,
    text,
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
    # `none_as_null=True` is load-bearing: assigning Python `None` writes
    # SQL `NULL`, not the JSON `null` literal. Without this, `signal.ai_analysis
    # = None` would persist as a JSON-null *value* (NOT NULL at the SQL level)
    # and the AI-backfill filter (`ai_analysis IS NULL` in
    # `pipeline.ai_backfill.backfill_null_ai`) would silently skip the row.
    # The default INSERT path in `signal_builder` omits the column entirely
    # so the DB default (SQL NULL) lands, but any explicit `signal.ai_analysis
    # = None` (e.g. in tests, future "reset this row" tooling, or a hypothetical
    # "force re-analysis" admin action) MUST resolve to SQL NULL or it gets
    # stranded outside the backfill query forever.
    ai_analysis: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    confidence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default=SignalStatus.PENDING, nullable=False)
    price_at_creation: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
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
    entry_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    stop_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    target_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    # Hardcoded "v1" prior to Stage 07; bumped only on prompt-structure changes
    # that affect AI calibration (not wording edits). Used by the track-record
    # query to compare apples-to-apples across prompt revisions (issue #32).
    ai_prompt_version: Mapped[str] = mapped_column(String(10), nullable=False, server_default="v1")
    # PR I.2 — cross-factor confirmation score. Count of orthogonal factors
    # (price / regime / news) whose direction agrees with the AI's
    # `suggested_action`. NULL when:
    #   - AI didn't run (`confidence IS NULL`); no direction to confirm.
    #   - `suggested_action == "wait"`; no directional claim to validate.
    # Range 0..3 (CHECK); v1 news always votes 0, so realistic max is 2 until
    # v2 news-sentiment ships. `factor_votes` carries the structured per-
    # factor breakdown ({"price": {"vote": ±1|0, "reason": ...}, ...}) for
    # audit + frontend "why" rendering.
    # D23: confirmation_score is set ONCE per row and never updated. Three
    # write paths share the invariant: `signal_builder.build_signal` (fresh
    # insert), `ai_backfill.backfill_null_ai` (after late AI success — same
    # one-write-per-row because the row was NULL before), and
    # `pipeline.confirmation_backfill.backfill_null_confirmation` (historical
    # NULL fill for pre-I.2 rows). Re-scoring would silently rewrite history
    # because the inputs (regime snapshot, price window) are point-in-time.
    confirmation_score: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    factor_votes: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
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
        CheckConstraint(
            "status IN ('pending','alerted','expired')",
            name="ck_signals_status_enum",
        ),
        CheckConstraint(
            "confirmation_score IS NULL OR confirmation_score BETWEEN 0 AND 3",
            name="ck_signals_confirmation_score_range",
        ),
        Index("ix_signals_fingerprint_date", "fingerprint", "signal_date", unique=True),
        Index("ix_signals_created", "created_at"),
        Index("ix_signals_asset_created", "asset", "created_at"),
        Index("ix_signals_expires", "expires_at"),
        # Partial index — see migration for rationale. Skips the many NULL
        # rows (legacy + AI-failed + wait signals) so the index size tracks
        # only signals where the delivery filter actually fires.
        Index(
            "ix_signals_confirmation",
            "confirmation_score",
            postgresql_where=text("confirmation_score IS NOT NULL"),
        ),
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
        BigInteger,
        ForeignKey("signals.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    asset: Mapped[str] = mapped_column(String(10), nullable=False)
    signal_type: Mapped[str] = mapped_column(String(30), nullable=False)
    direction: Mapped[str] = mapped_column(String(10), nullable=False)
    confidence: Mapped[int] = mapped_column(Integer, nullable=False)
    entry_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    stop_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    target_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    price_at_signal: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    price_after_24h: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    price_after_72h: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    hit_target: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    hit_stop: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    max_favorable: Mapped[Decimal | None] = mapped_column(Numeric(8, 4), nullable=True)
    max_adverse: Mapped[Decimal | None] = mapped_column(Numeric(8, 4), nullable=True)
    # Issue #60 scaffolding — populated by the v2 outcome evaluator (PR B).
    # NULL on rows written before PR B = "scored against the legacy 72h
    # window." `scoring_version` follows the same `^v[0-9]+$` shape as
    # `Signal.ai_prompt_version` for symmetry between version-tagged columns;
    # `window_hours` is the per-signal scoring window derived from the AI's
    # `time_horizon` (scalp 6h / swing 72h / position 168h);
    # `price_at_validity_end` is the close at t0 + window_hours, the
    # horizon-aware analogue of `price_after_72h`. All three are NULL until
    # PR B's writer lands; PR A only declares the columns.
    window_hours: Mapped[int | None] = mapped_column(Integer, nullable=True)
    scoring_version: Mapped[str | None] = mapped_column(String(8), nullable=True)
    price_at_validity_end: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
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
