"""Response shape for `GET /api/analytics/breakdown`.

Stage 8-P10. Pydantic projection of the frozen dataclasses in
`pipeline.analytics` — same field names + semantics, but with HTTP-layer
concerns (validation bounds, OpenAPI metadata, `extra="forbid"`) layered
on top.

Why two-layer (dataclass + Pydantic) instead of returning Pydantic from
the pipeline:
    Per CLAUDE.md, domain packages (`models/`, `pipeline/`, …) must stay
    HTTP-agnostic — they don't import from `etfpulse.api`. Pydantic IS an
    HTTP concern here (response_model serialization, OpenAPI). Keeping
    the pipeline output as plain dataclasses means a future bot caller
    (`/analytics` Telegram command) can consume the SAME pipeline function
    without dragging Pydantic into the bot dispatch path. The cost is one
    short conversion function in `routes/analytics.py` — well worth the
    layer-clean separation.

Why not Pydantic's `from_attributes=True` to auto-convert:
    The pipeline dataclasses use `slots=True` which doesn't expose `__dict__`
    cleanly; `from_attributes` works on instance attributes but the explicit
    `.from_dataclass(...)` classmethods below make the projection visible
    in one place. If a field gets added, the type checker flags every
    classmethod that's missing it.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from etfpulse.pipeline.analytics import (
    BreakdownRow,
    HistogramBucket,
    TrackRecordBreakdown,
)


class BreakdownStat(BaseModel):
    """One row of a categorical breakdown (detector / asset / direction /
    confidence-bucket).

    Mirrors `pipeline.analytics.BreakdownRow`. `hit_rate_pct` is None when
    `targeted == 0` — same null-vs-zero convention as elsewhere in this
    codebase. The FE renders the None as "—" or "pending", NOT as "0%".
    """

    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1)
    total: int = Field(ge=0)
    # Subset of `total` where the underlying signal had a target_price —
    # the denominator for hit_rate. Can be < total when some signals had
    # NULL target (AI declined to set one).
    targeted: int = Field(ge=0)
    hits: int = Field(ge=0)
    # 0..100, rounded to 2dp by `compute_hit_rate_pct`. None when cohort
    # has no targeted signals (see class docstring).
    hit_rate_pct: float | None = Field(default=None, ge=0.0, le=100.0)

    @classmethod
    def from_row(cls, row: BreakdownRow) -> BreakdownStat:
        """Project a pipeline `BreakdownRow` dataclass into the HTTP DTO.
        Centralised here so a new field on the dataclass surfaces as a
        type error at exactly one site."""
        return cls(
            label=row.label,
            total=row.total,
            targeted=row.targeted,
            hits=row.hits,
            hit_rate_pct=row.hit_rate_pct,
        )


class HistogramBucketOut(BaseModel):
    """One bin of the MFE/MAE histogram.

    `lower` is inclusive (fraction; 0.005 = 0.5%); `upper` is exclusive.
    The final bucket's `upper` is None — open-ended ≥-the-last-finite-edge.
    The FE renders the bin label as the user-facing axis tick; `lower` /
    `upper` are exposed so a hover tooltip can show exact boundaries.
    """

    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1)
    # Both bounds are fractions in [0, 1+) — i.e. 0.025 means 2.5%. Kept
    # as fractions (not percent integers) to match the underlying
    # Decimal storage on `SignalOutcome.max_favorable / max_adverse`;
    # converting at render-time keeps a single canonical unit on the wire.
    lower: float = Field(ge=0.0)
    upper: float | None = Field(default=None, ge=0.0)
    count: int = Field(ge=0)

    @classmethod
    def from_bucket(cls, bucket: HistogramBucket) -> HistogramBucketOut:
        return cls(
            label=bucket.label,
            lower=bucket.lower,
            upper=bucket.upper,
            count=bucket.count,
        )


class TrackRecordBreakdownOut(BaseModel):
    """Full diagnostic surface for `/analytics`.

    All breakdowns share the same row shape so the frontend can render
    them with one component. The two histograms share the same bucket
    shape for the same reason.

    `total_outcomes` is the global denominator captioned at the top of
    the page ("Based on N evaluated signals") — answers the "how
    statistically thin is this?" question that every track-record reader
    asks. Cold-boot returns zero and the FE shows the empty state.
    """

    model_config = ConfigDict(extra="forbid")

    total_outcomes: int = Field(ge=0)
    by_detector: list[BreakdownStat]
    by_asset: list[BreakdownStat]
    by_confidence_bucket: list[BreakdownStat]
    by_direction: list[BreakdownStat]
    mfe_histogram: list[HistogramBucketOut]
    mae_histogram: list[HistogramBucketOut]

    @classmethod
    def from_breakdown(cls, breakdown: TrackRecordBreakdown) -> TrackRecordBreakdownOut:
        """Single projection point — the route calls this exactly once.
        If the dataclass gains a field, this method fails to type-check
        until the DTO is updated, which is the desired drift guard."""
        return cls(
            total_outcomes=breakdown.total_outcomes,
            by_detector=[BreakdownStat.from_row(r) for r in breakdown.by_detector],
            by_asset=[BreakdownStat.from_row(r) for r in breakdown.by_asset],
            by_confidence_bucket=[
                BreakdownStat.from_row(r) for r in breakdown.by_confidence_bucket
            ],
            by_direction=[BreakdownStat.from_row(r) for r in breakdown.by_direction],
            mfe_histogram=[HistogramBucketOut.from_bucket(b) for b in breakdown.mfe_histogram],
            mae_histogram=[HistogramBucketOut.from_bucket(b) for b in breakdown.mae_histogram],
        )
