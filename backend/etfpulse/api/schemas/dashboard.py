"""Response DTO for `GET /api/dashboard/stats`.

Pure value object — the aggregation query lives in the route. `hit_rate_72h`
is intentionally NOT here until Stage 08 (depends on `SignalOutcome` rows
which don't exist yet; see open_issues.md #44).
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
