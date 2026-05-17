"""Response shapes for `GET /api/track-record`.

Stage 8-P4. Mirrors the structure of `schemas/signals.py:PaginatedSignals`
so frontend pagination components can be shared between the signals feed
and the track-record list (both use cursor + page modes against
`(timestamp, id)` cursors).

Why a separate `TrackRecordItemOut` rather than reusing `SignalOutcomeOut`:
    `SignalOutcomeOut` is the NESTED shape inside `SignalDetail` — it carries
    only outcome columns (entry/stop/target/price_at_signal/...). The
    track-record list needs those PLUS a few denormalized columns
    (`signal_id`, `asset`, `signal_type`, `direction`, `confidence`) so
    each row is renderable on its own without a JOIN. Inheriting one from
    the other crossed our "two callers ≠ premature abstraction" line.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# Single source of truth for the horizon bucket literal. Mirrors
# `pipeline.track_record.HorizonLabel` (the Python-side type) and gets
# imported by every API surface that filters or returns horizon-labelled
# data (track-record route, calibration route). Defining it here keeps
# the two literal-string lists from drifting; a future addition (e.g.
# an "intraday" bucket once #62 lands) updates one place.
HorizonLiteral = Literal["scalp", "swing", "position", "legacy"]


class TrackRecordSummary(BaseModel):
    """Aggregate stats over the SAME filter set as the paginated `items`.

    All counts are over `signal_outcomes` rows (i.e. signals that have been
    evaluated by `pipeline.track_record`). Signals without an outcome row
    yet are NOT in `total_evaluated` — by design, the public track record
    only shows scored signals.

    `hit_rate_pct` divides `targets_hit / targeted_count`, NOT
    `targets_hit / total_evaluated`. Without this distinction, a signal
    whose AI declined to volunteer a target (`hit_target IS NULL`) would
    drag the denominator and falsely deflate the hit rate. The FE renders
    "X% of signals with targets hit them" rather than "X% of all signals".
    """

    model_config = ConfigDict(extra="forbid")

    total_evaluated: int = Field(ge=0)
    targets_hit: int = Field(ge=0)
    stops_hit: int = Field(ge=0)
    # Subset of `total_evaluated` where the AI set a target — i.e. the
    # signals where "did we hit the target?" is even a meaningful question.
    # `targets_hit / targeted_count` is the canonical hit_rate denominator.
    targeted_count: int = Field(ge=0)
    # 0..100. None when `targeted_count == 0` — better than rendering "0%"
    # for an empty cohort. Mixed-horizon (legacy semantics) — for the
    # honest per-horizon view, read `hit_rate_by_horizon` below.
    hit_rate_pct: float | None = Field(default=None, ge=0.0, le=100.0)
    # PR B (#60) — bucketed hit rate. Keys are "scalp" / "swing" /
    # "position" / "legacy". Each value is null when that bucket has no
    # targeted signals (same null-vs-zero convention as `hit_rate_pct`).
    # The bucketing question is "what window was each signal scored
    # against?" — `legacy` is rows pre-PR-B (NULL `scoring_version`,
    # always scored against a fixed 72h). New cohorts are bucketed by
    # their AI-stated `time_horizon`; until intraday kline support lands
    # (#62), scalp signals are skipped at evaluation so the "scalp" key
    # will be `null` for the foreseeable future.
    hit_rate_by_horizon: dict[str, float | None] = Field(default_factory=dict)
    # Average `confidence` (1..10) of the signals that hit / didn't hit
    # their target. Useful for showing "high-confidence signals win more
    # often". None when the respective bucket is empty.
    avg_confidence_hits: float | None = Field(default=None, ge=1.0, le=10.0)
    avg_confidence_misses: float | None = Field(default=None, ge=1.0, le=10.0)


class TrackRecordItemOut(BaseModel):
    """One row in the paginated track-record list.

    Floats (not Decimal) for prices to match the convention already in
    `SignalOutcomeOut` / frontend `SignalOutcome` — uniform across the API
    surface so the frontend doesn't deserialise two different number types.
    """

    model_config = ConfigDict(from_attributes=True, extra="ignore")

    id: int
    signal_id: int
    asset: str
    signal_type: str
    direction: str
    confidence: int = Field(ge=1, le=10)

    entry_price: float | None = None
    stop_price: float | None = None
    target_price: float | None = None
    # PR I.3b — nullable for MARKET (regime_shift) outcomes; single-asset
    # outcomes always populate it. FE treats null as "composite-scored,
    # no single-asset baseline."
    price_at_signal: float | None = None
    price_after_24h: float | None = None
    price_after_72h: float | None = None
    # PR B (#60) — see `SignalOutcomeOut` for the field semantics; same
    # three fields, same NULL-means-legacy convention.
    price_at_validity_end: float | None = None
    window_hours: int | None = None
    scoring_version: str | None = None

    # Tri-state — `True` hit, `False` did not hit, `None` no level set.
    # See `pipeline/track_record._compute_metrics` for the semantics.
    hit_target: bool | None = None
    hit_stop: bool | None = None

    # Unsigned fractions of entry (e.g. 0.032 = 3.2%). Asset-agnostic.
    max_favorable: float | None = None
    max_adverse: float | None = None

    # PR I.3b — see SignalOutcomeOut.composite_return_pct. NULL on
    # single-asset rows; populated on MARKET (regime_shift) outcomes.
    composite_return_pct: float | None = None

    evaluated_at: datetime


class PaginatedTrackRecord(BaseModel):
    """Envelope mirroring `PaginatedSignals` so the frontend can reuse the
    same pager component shape for both endpoints."""

    model_config = ConfigDict(extra="forbid")

    summary: TrackRecordSummary
    items: list[TrackRecordItemOut]
    # Cursor pagination — pass back as `?cursor=` to fetch the next page.
    # None when there's no next page.
    next_cursor: str | None = None
    # Total rows matching the current filter set (populated in BOTH modes,
    # same as `/api/signals`).
    total: int = Field(ge=0)
    # 1-based page number, or null in cursor mode.
    page: int | None = Field(default=None, ge=1)
    # ceil(total / limit). 0 when total == 0.
    total_pages: int = Field(ge=0)
