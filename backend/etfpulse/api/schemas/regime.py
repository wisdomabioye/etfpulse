"""Response DTO for `GET /api/regime`.

Mirrors `RegimeSnapshot` row shape projected for the public surface. The
classifier's reasoning JSONB is passed through verbatim so the frontend
(and any future Telegram formatter) can render the structured score
breakdown without re-deriving it server-side. Pre-baking bullet strings
here would either lock display formatting or force the frontend to
re-parse them — both worse than pass-through.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

# Mirrors the DB CHECK constraint `ck_regime_snapshots_regime_enum` and the
# `MarketRegime` StrEnum. Pinning as a Literal at the response layer makes
# a typo'd classifier output fail at serialize time (500) instead of
# silently sneaking through the column constraint.
RegimeLiteral = Literal["accumulation", "markup", "distribution", "markdown", "uncertain"]

# Mirrors `ck_regime_snapshots_posture_enum` + `SignalPosture` StrEnum.
PostureLiteral = Literal["aggressive", "normal", "cautious", "paused"]


class RegimeResponse(BaseModel):
    """Latest market regime classification.

    Sourced from `regime_snapshots` — the single most recent row, ordered
    by `captured_at DESC`. The endpoint 503s when the table is empty
    (cold-boot before the first daily cycle has run).

    The route constructs this with explicit kwargs (not `model_validate`)
    because it has to unwrap `macro_events` JSONB and narrow nullable
    columns; `from_attributes` would not help here, so it's intentionally
    omitted.
    """

    # `regime` and `signal_posture` are nullable on the column (legacy
    # snapshots predate Stage 7 classifier output) but populated on every
    # row the classifier writes. The endpoint short-circuits to 503 if the
    # latest row has NULL regime, so consumers see strings here, never
    # null. Tightened to Literals so the column CHECK constraint and the
    # response schema enforce the same value set.
    regime: RegimeLiteral
    signal_posture: PostureLiteral
    confidence: int = Field(ge=1, le=10)

    # Structured score breakdown — pass-through of `regime_snapshots.reasoning`
    # JSONB. Schema is intentionally open per regime_monitor.py: the classifier
    # owns it; consumers must tolerate missing keys. Top-level keys today:
    # `score`, `flow`, `news`, `macro`, `dominance` — see regime_monitor.py.
    reasoning: dict[str, Any] = Field(default_factory=dict)

    # Flat list of nearby macro-event labels (e.g. ["FOMC", "CPI"]) — sourced
    # from the `events_nearby` key of `regime_snapshots.macro_events` JSONB
    # (wrapper key documented as `REGIME_MACRO_EVENTS_KEY` in signal_builder).
    # Empty list when no events are within the window.
    macro_events_nearby: list[str] = Field(default_factory=list)

    # `regime_snapshots.captured_at` — UTC, timezone-aware.
    classified_at: datetime
