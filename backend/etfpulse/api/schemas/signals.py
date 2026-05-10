"""Response DTOs for `/api/signals` endpoints.

The API contract is deliberately decoupled from the internal `ai_analysis`
JSONB structure — we re-declare `AIAnalysisOut` rather than reusing
`pipeline.analysis.AISignalAnalysis`. If an internal AI field is renamed,
the API surface stays stable.

Flattening strategy: `SignalListItem.from_row` is a factory classmethod that
takes the ORM signal + (outcome_id, alerted_to, now) and produces the DTO.
The display status is derived (evaluated > expired > raw DB status), keeping
the frontend simple and avoiding a backend enum change.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from etfpulse.models import Signal, SignalOutcome

# Display statuses the frontend renders. `evaluated` is DERIVED (outcome
# exists), not a DB enum value — see `_derive_display_status` below.
DisplayStatus = Literal["pending", "alerted", "evaluated", "expired"]

# Shared FastAPI Query() Literals — multiple route modules
# (`routes/signals.py`, `routes/track_record.py`) accept the same asset +
# signal_type filter values. Hosting them HERE (next to the canonical
# `Signal`-shape DTOs) means a new asset / signal type is a one-line edit
# in one file, not a coordinated multi-file change. Stage 8-P6 cross-cut
# review extracted these from the per-route declarations they had drifted
# into.
AssetLiteral = Literal["BTC", "ETH"]
SignalTypeLiteral = Literal[
    "flow_anomaly", "magnitude", "acceleration", "divergence", "regime_shift"
]


class AIAnalysisOut(BaseModel):
    """Nested AI analysis in the detail response.

    Fields are optional because an LLM failure at signal-build time leaves
    `Signal.ai_analysis = NULL`. `SignalDetail.ai_analysis` itself becomes
    None in that case; this model is for when it IS populated.

    Stage 8-P1 added entry/stop/target — these mirror the columns now on
    `Signal` (kept here for the natural "what the AI said" detail shape on
    the frontend; the columns are for outcome-evaluator queries). Float
    serialization matches `SignalOutcomeOut` for frontend type-uniformity.
    """

    model_config = ConfigDict(extra="ignore")

    headline: str
    reasoning: list[str] = Field(default_factory=list)
    confidence: int = Field(ge=1, le=10)
    risks: list[str] = Field(default_factory=list)
    suggested_action: str
    time_horizon: str
    entry_price: float | None = None
    stop_price: float | None = None
    target_price: float | None = None


class SignalOutcomeOut(BaseModel):
    """Signal outcome — populated in Stage 08 when 24h/72h evaluation runs.

    For Phase 1 this is always None on every signal detail response. We
    define the shape now so Stage 08 can drop outcome rows in without any
    frontend or contract change.
    """

    model_config = ConfigDict(from_attributes=True)

    entry_price: float | None = None
    stop_price: float | None = None
    target_price: float | None = None
    price_at_signal: float
    price_after_24h: float | None = None
    price_after_72h: float | None = None
    hit_target: bool | None = None
    hit_stop: bool | None = None
    max_favorable: float | None = None
    max_adverse: float | None = None
    evaluated_at: datetime | None = None


class SignalListItem(BaseModel):
    """Lightweight projection for the feed + home-page "recent signals" list.

    Drops `ai_analysis.reasoning` + `ai_analysis.risks` arrays (detail-only)
    to keep list responses small and fast to serialize for 20-item pages.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    asset: str
    signal_type: str
    status: DisplayStatus
    confidence: int | None = Field(default=None, ge=1, le=10)

    # Flattened from ai_analysis JSONB — NULL when AI failed at build time.
    headline: str | None = None
    suggested_action: str | None = None
    time_horizon: str | None = None

    signal_date: date
    created_at: datetime
    expires_at: datetime | None = None

    # COUNT of SignalDelivery rows — "attempted" count per edge-case decision.
    alerted_to: int

    @classmethod
    def from_row(
        cls,
        signal: Signal,
        *,
        outcome_id: int | None,
        alerted_to: int,
        now: datetime,
    ) -> SignalListItem:
        """Build a DTO from an ORM row + precomputed scalars.

        Called from the list endpoint's per-row loop after a single JOINed
        query materializes outcome_id and alerted_to for every signal.
        """
        ai: dict[str, Any] = signal.ai_analysis or {}
        return cls(
            id=signal.id,
            asset=signal.asset,
            signal_type=signal.signal_type,
            status=_derive_display_status(signal, outcome_id=outcome_id, now=now),
            confidence=signal.confidence,
            headline=ai.get("headline"),
            suggested_action=ai.get("suggested_action"),
            time_horizon=ai.get("time_horizon"),
            signal_date=signal.signal_date,
            created_at=signal.created_at,
            expires_at=signal.expires_at,
            alerted_to=alerted_to,
        )


class SignalDetail(BaseModel):
    """Full single-signal response for `/api/signals/{id}`.

    Includes the raw `trigger_data` JSONB (detectors' own record of what
    they saw) + nested `ai_analysis` + outcome (None until Stage 08).
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    asset: str
    signal_type: str
    status: DisplayStatus
    confidence: int | None = Field(default=None, ge=1, le=10)
    fingerprint: str  # Full 32-char — frontend truncates to 8 for display.
    signal_date: date
    created_at: datetime
    expires_at: datetime | None = None
    alerted_to: int

    trigger_data: dict[str, Any]
    ai_analysis: AIAnalysisOut | None = None
    outcome: SignalOutcomeOut | None = None

    @classmethod
    def from_row(
        cls,
        signal: Signal,
        *,
        outcome: SignalOutcome | None,
        alerted_to: int,
        now: datetime,
    ) -> SignalDetail:
        ai_analysis: AIAnalysisOut | None = None
        if signal.ai_analysis:
            # `model_validate` rather than `**signal.ai_analysis` so Pydantic
            # applies its defaults + extra="ignore" policy cleanly.
            ai_analysis = AIAnalysisOut.model_validate(signal.ai_analysis)

        return cls(
            id=signal.id,
            asset=signal.asset,
            signal_type=signal.signal_type,
            status=_derive_display_status(
                signal, outcome_id=outcome.id if outcome else None, now=now
            ),
            confidence=signal.confidence,
            fingerprint=signal.fingerprint,
            signal_date=signal.signal_date,
            created_at=signal.created_at,
            expires_at=signal.expires_at,
            alerted_to=alerted_to,
            trigger_data=signal.trigger_data or {},
            ai_analysis=ai_analysis,
            outcome=SignalOutcomeOut.model_validate(outcome) if outcome else None,
        )


class PaginatedSignals(BaseModel):
    """Envelope for `GET /api/signals` list responses.

    Two pagination modes share this shape:
      - **Cursor-based** (default): `next_cursor` is `"iso_timestamp|id"` or
        None at end. Pass back via `?cursor=`. Best for infinite-scroll feeds.
        `page` is `None` in this mode — cursor traversal has no page number.
      - **Page-based**: `page` (1-based) populated when `?page=N` was sent.
        Best for numbered pagers.

    `total` and `total_pages` are always populated regardless of mode so a
    UI can render "Showing N of M" without conditional logic. `total` is a
    single COUNT roundtrip per list call — acceptable at the signal table's
    scale (10s of thousands max).
    """

    items: list[SignalListItem]
    next_cursor: str | None = None
    total: int = 0
    # Null in cursor mode (cursor traversal is not page-numbered); 1-based int
    # when `?page=N` was supplied.
    page: int | None = None
    total_pages: int = 0


def _derive_display_status(
    signal: Signal, *, outcome_id: int | None, now: datetime
) -> DisplayStatus:
    """Three-tier precedence for the frontend-visible status:

        1. `evaluated` — if a SignalOutcome row exists (Stage 08 product)
        2. `expired`   — if `expires_at` is past `now`
        3. raw DB status (pending / alerted / expired)

    Rationale: outcome is the most meaningful state for users ("we measured
    this"). Past-expiry wins over raw DB status so a signal the reaper
    hasn't visited yet still renders as "expired" to the user. The raw DB
    status is the fallback. All four branches return a DisplayStatus Literal.
    """
    if outcome_id is not None:
        return "evaluated"
    if signal.expires_at is not None and signal.expires_at < now:
        return "expired"
    # Raw DB status is already lowercase via StrEnum.
    raw = signal.status
    # Narrow to Literal — defensive; DB enum should never yield anything else.
    if raw in ("pending", "alerted", "expired"):
        return raw  # type: ignore[return-value]
    return "pending"  # fallback; should be unreachable


def format_cursor(created_at: datetime, id: int) -> str:
    """Composite cursor string: `"iso_timestamp|id"` with UTC as `Z`
    (NOT `+00:00`) — `+` is reserved in URL form-encoding and gets decoded
    back to a space, breaking round-trip through the query string. The
    ID component breaks ties when two signals share the same `created_at`
    (possible on same-millisecond bulk fan-outs).
    """
    iso = created_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return f"{iso}|{id}"


def parse_cursor(raw: str) -> tuple[datetime, int] | None:
    """Inverse of `format_cursor`. Returns None on any parse failure so the
    endpoint can 422 cleanly instead of 500-ing on malformed input."""
    try:
        ts_str, id_str = raw.rsplit("|", 1)
        # fromisoformat in Python 3.11+ accepts the `Z` suffix natively.
        return datetime.fromisoformat(ts_str), int(id_str)
    except (ValueError, AttributeError):
        return None
