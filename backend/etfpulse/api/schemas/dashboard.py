"""Response DTOs for `GET /api/dashboard/stats`.

Pure value objects — the aggregation queries live in the route.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field


class HeroOutcome(BaseModel):
    """Single closed `SignalOutcome` surfaced on the home page hero card.

    Two slots populated independently in `DashboardStats`:
      * `last_target_hit`  — most recently evaluated outcome with hit_target=True
      * `last_stop_saved`  — most recently evaluated outcome with hit_stop=True

    `max_favorable` and `max_adverse` are **unsigned fractions of entry**
    (e.g. `0.05` for 5%), matching the column semantics in
    `pipeline/track_record.py:_compute_metrics`. The FE multiplies by 100
    for display. Naming intentionally drops the `_pct` suffix used in earlier
    drafts — calling a fraction "pct" would mislead future readers.

    `entry_price`, `stop_price`, `target_price` carry the suggested levels
    from the signal at creation time; the FE can compute the actual gain to
    target (or stop loss) without an extra API call.

    `headline` is sourced from `signal.ai_analysis["headline"]` for cards
    that want a one-line description. None only if AI failed at build time,
    which can't happen for rows surfacing here — hit_target/hit_stop both
    require AI-set levels.
    """

    signal_id: int
    asset: str
    signal_type: str
    direction: str  # "long" | "short"
    confidence: int = Field(ge=1, le=10)
    headline: str | None = None

    entry_price: Decimal
    stop_price: Decimal | None = None
    target_price: Decimal | None = None
    price_at_signal: Decimal

    max_favorable: Decimal | None = None
    max_adverse: Decimal | None = None

    evaluated_at: datetime
    signal_created_at: datetime


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

    # PR E.1 — hero card slots. Both None on cold-start or when no
    # qualifying outcome exists. The FE renders the aggregate strip
    # alone in that case — no hollow placeholder. Rotation between the
    # two cards is an FE concern (see PR E.2 / task #29).
    last_target_hit: HeroOutcome | None = None
    last_stop_saved: HeroOutcome | None = None
