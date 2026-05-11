"""AISignalAnalysis — the typed shape of an LLM response on a detector hit.

Lives in `pipeline/` (not `api/schemas/` or `models/`) because it's an
internal data shape: produced by the OpenRouter adapter, consumed by
`signal_builder`, and persisted as JSONB into `Signal.ai_analysis`. It is
neither an HTTP request/response shape nor a DB column.

Design philosophy: be FORGIVING with LLM output. The validators clamp,
truncate, and strip rather than reject — a partial analysis is better than
no signal. Strict validation is for human/system inputs, not model outputs.
Resolution R18: clamp confidence to [1,10], truncate reasoning to 5 items,
risks to 3 items.

Prompt versioning (issue #32, closed by Stage 7-P6):
    AI_PROMPT_VERSION is the string stamped onto every Signal at insert
    time so the track-record query can compare apples-to-apples across
    prompt revisions. Bump ONLY when the prompt's CONTEXT SHAPE changes —
    adding/removing sections (e.g. v1 → v2 added regime + news_context),
    changing the confidence rubric, or reworking how trigger_data is
    framed. Small wording edits (typos, grammar, polish) DO NOT bump —
    they don't change calibration. The CHECK constraint
    `ck_signals_ai_prompt_version_format` (`^v[0-9]+$`) is the wire
    format; keep this string compatible.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

if TYPE_CHECKING:
    # Type-only import — avoids a runtime cycle (`models` imports from
    # `pipeline` only via TYPE_CHECKING too). The helper below mutates a
    # Signal in-place; callers always pass a real ORM row.
    from etfpulse.models import Signal

# Time horizon → signal validity window. Resolution R16. These set
# `Signal.expires_at` so the dashboard can hide stale signals.
_HORIZON_TO_DURATION: dict[str, timedelta] = {
    "scalp": timedelta(hours=6),
    "swing": timedelta(hours=72),
    "position": timedelta(days=7),
}

# Soft cap on headline length. Telegram messages can hold 4096 chars but a
# headline of more than ~200 chars would crowd out the body of the alert.
# Validator truncates with an ellipsis suffix rather than rejecting.
_HEADLINE_MAX_LEN = 200

# Resolution R18 — keep AI output digestible, don't let an over-eager model
# flood the alert with 20 reasoning bullets.
_REASONING_MAX_ITEMS = 5
_RISKS_MAX_ITEMS = 3


# Stamped onto Signal.ai_prompt_version at insert time. See module docstring
# for the bump policy. Match the regex `^v[0-9]+$` (DB CHECK constraint).
#
# v1 → v2 (Stage 7-P6): added regime + news_context sections.
# v2 → v3 (Stage 8-P1): added entry_price/stop_price/target_price to the
#                       response schema.
AI_PROMPT_VERSION = "v3"


class AISignalAnalysis(BaseModel):
    # `extra="ignore"` is the explicit, intentional choice — LLMs occasionally
    # add chatty fields ("thinking", "caveat") and we'd rather drop them than
    # 422 the whole response.
    model_config = ConfigDict(extra="ignore")

    headline: str
    reasoning: list[str]
    confidence: int = Field(ge=1, le=10)
    risks: list[str]
    suggested_action: Literal["consider long", "consider short", "wait"]
    time_horizon: Literal["scalp", "swing", "position"]
    # Stage 8-P1 — AI-suggested price levels. Decimal not float to match
    # the Signal columns (Numeric) and dodge JSON-roundtrip precision drift
    # that would corrupt downstream hit/stop comparisons. All three are
    # nullable: a "wait" suggestion has no entry/stop/target, and the model
    # may legitimately decline to suggest specific levels on weak signals.
    # Validators below FORCE all three to None when suggested_action="wait"
    # (cleans up models that suggest "wait" but still volunteer numbers)
    # and clamp non-positive values to None (negative price = nonsense).
    entry_price: Decimal | None = None
    stop_price: Decimal | None = None
    target_price: Decimal | None = None

    @field_validator("confidence", mode="before")
    @classmethod
    def _clamp_confidence(cls, v: object) -> int:
        # Clamp BEFORE the Field(ge=1, le=10) check fires so the field
        # constraints become assertions of post-clamp invariants rather
        # than gates on raw LLM output.
        if isinstance(v, int):
            n = v
        else:
            try:
                n = int(v)  # type: ignore[call-overload]
            except (TypeError, ValueError):
                return 1
        return max(1, min(10, n))

    @field_validator("headline", mode="after")
    @classmethod
    def _trim_headline(cls, v: str) -> str:
        v = v.strip()
        if len(v) > _HEADLINE_MAX_LEN:
            # `... ` ellipsis fits the prefix; subtract its width from the cap.
            return v[: _HEADLINE_MAX_LEN - 1].rstrip() + "…"
        return v

    @field_validator("reasoning", mode="after")
    @classmethod
    def _clean_reasoning(cls, v: list[str]) -> list[str]:
        return _strip_drop_truncate(v, _REASONING_MAX_ITEMS)

    @field_validator("risks", mode="after")
    @classmethod
    def _clean_risks(cls, v: list[str]) -> list[str]:
        return _strip_drop_truncate(v, _RISKS_MAX_ITEMS)

    @field_validator("entry_price", "stop_price", "target_price", mode="before")
    @classmethod
    def _coerce_price(cls, v: object) -> Decimal | None:
        """Forgive LLM price encodings: `"84200.5"` strings, ints, or floats
        all coerce to Decimal. Non-positive becomes None (not a ValidationError —
        the rest of the analysis is still useful). Unparseable becomes None.

        NaN/Infinity also become None — both are valid `Decimal()` constructor
        inputs but Postgres NUMERIC rejects them on INSERT, AND `NaN <= 0`
        raises `decimal.InvalidOperation` (would crash this validator if not
        caught upstream). `is_finite()` short-circuits both before we reach
        the comparison.
        """
        if v is None:
            return None
        try:
            d = v if isinstance(v, Decimal) else Decimal(str(v))
        except (TypeError, ValueError, ArithmeticError):
            return None
        if not d.is_finite() or d <= 0:
            return None
        return d

    @model_validator(mode="after")
    def _drop_prices_on_wait(self) -> AISignalAnalysis:
        """A `wait` suggestion implies no actionable trade — drop any prices
        the model volunteered. Otherwise the Telegram formatter would render
        "Suggested: WAIT at $84,200" which is contradictory.

        Done at model_validator (not on suggested_action's field validator)
        because the price fields are validated independently — we need both
        sides resolved before we can decide to clear."""
        if self.suggested_action == "wait":
            self.entry_price = None
            self.stop_price = None
            self.target_price = None
        return self


def _strip_drop_truncate(items: list[str], cap: int) -> list[str]:
    """Strip whitespace, drop empty entries, then truncate to cap.

    Order matters: dropping empties BEFORE truncation means the cap counts
    real content. Otherwise `["", "", "real reason"]` truncated to 5 keeps
    the two leading empties.
    """
    cleaned = [s.strip() for s in items]
    nonempty = [s for s in cleaned if s]
    return nonempty[:cap]


def apply_analysis_to_signal(signal: Signal, analysis: AISignalAnalysis) -> None:
    """Mirror an `AISignalAnalysis` onto its `Signal` row.

    The single source of truth for the AI → Signal field mapping. Every
    column that mirrors an analysis field is assigned HERE; both
    `signal_builder.build_signal` (fresh-insert path) and
    `ai_backfill.backfill_null_ai` (retry path) call this so adding a new
    mirrored column is a one-line edit in one place rather than a
    coordinated two-file change that drifts.

    Mutation, not return — the Signal is already attached to the caller's
    session and the caller controls flush/commit (D14).
    """
    signal.ai_analysis = analysis.model_dump(mode="json")
    signal.confidence = analysis.confidence
    signal.expires_at = compute_expires_at(analysis.time_horizon)
    signal.entry_price = analysis.entry_price
    signal.stop_price = analysis.stop_price
    signal.target_price = analysis.target_price


def compute_expires_at(time_horizon: str, now: datetime | None = None) -> datetime:
    """Map a time_horizon label to a UTC-aware expiry datetime.

    The optional `now` parameter is for tests — pass a fixed datetime to
    avoid frozen-time fixtures. Production callers omit it. Resolution R16.

    Raises ValueError on an unknown horizon — the AISignalAnalysis schema
    already restricts to a Literal, so reaching this branch means a caller
    bypassed validation.
    """
    if time_horizon not in _HORIZON_TO_DURATION:
        raise ValueError(
            f"unknown time_horizon {time_horizon!r}; expected one of {sorted(_HORIZON_TO_DURATION)}"
        )
    if now is None:
        now = datetime.now(UTC)
    return now + _HORIZON_TO_DURATION[time_horizon]
