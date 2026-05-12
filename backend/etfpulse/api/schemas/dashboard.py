"""Response DTO for `GET /api/dashboard/stats`.

Pure value object — the aggregation query lives in the route.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class DashboardStats(BaseModel):
    """Home page headline tiles. All fields tolerate the empty-DB case."""

    total_signals: int = Field(ge=0)
    signals_today: int = Field(ge=0)

    # None when no signals with non-NULL confidence exist (empty DB or all
    # AI-failed). Better than lying with 0.0 which would skew the display
    # ("our avg confidence is zero??").
    avg_confidence: float | None = Field(default=None, ge=1.0, le=10.0)

    # None on a truly empty DB. Otherwise the `created_at` of the most
    # recent signal (regardless of status — includes pending/alerted/expired).
    last_signal_at: datetime | None = None

    # Stage 7-P7: surface the latest regime classification on the home page
    # so the badge tile + TopNav indicator (#104) don't need a second
    # /api/regime roundtrip just to render the headline. Both fields are
    # null when no `regime_snapshots` row exists yet (cold-boot before the
    # first daily cycle) OR when the latest row is a pre-Stage-7 legacy
    # snapshot with NULL regime/posture columns. Frontend treats null as
    # "regime not yet classified" rather than rendering a hollow card.
    #
    # Field name `signal_posture` matches the column + classifier output +
    # /api/regime response — one canonical name for one concept across the
    # whole stack.
    current_regime: str | None = None
    signal_posture: str | None = None

    # PR B (#60) — `hit_rate_global` is the global hit rate over ALL
    # evaluated outcomes (no filters). Renamed from `hit_rate_72h` because
    # under the v2 rubric outcomes are scored against their OWN window
    # (scalp 6h / swing 72h / position 168h), not a fixed 72h. The number
    # is the same headline ("X% of targeted signals hit their target")
    # but the "72h" label was a misleading lie for the swing-and-non-swing
    # mixed cohort the value actually represents.
    #
    # Unit is PERCENT (0..100) — same as `/api/track-record.summary.hit_rate_pct`
    # so the FE never has to convert between fraction and percent. Null
    # when `evaluated_count == 0` OR when no signal that had a target was
    # scored — better than rendering "0%" for an empty cohort.
    #
    # Denominator is `targeted_count` (signals where the AI set a target),
    # NOT `total_evaluated` — same rationale as the track-record endpoint:
    # signals where AI declined a target shouldn't dilute the rate.
    hit_rate_global: float | None = Field(default=None, ge=0.0, le=100.0)
    # DEPRECATED — same value as `hit_rate_global`, kept for one release
    # cycle so a pinned-old frontend deploy doesn't 422 on the response
    # shape. Drop after the v2 frontend is the only consumer in production.
    # (CLAUDE.md rollback invariant — field REMOVE merges only after the
    # last code that reads it has been rolled out for at least one cycle.)
    hit_rate_72h: float | None = Field(default=None, ge=0.0, le=100.0)
    # Total SignalOutcome rows scored. Captioned next to hit_rate_global
    # ("on N evaluated signals"). 0 when the eval job hasn't produced any
    # outcome rows yet (cold-boot before signals age past 72h).
    evaluated_count: int = Field(default=0, ge=0)
