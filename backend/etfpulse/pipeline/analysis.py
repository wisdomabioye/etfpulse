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
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

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


def _strip_drop_truncate(items: list[str], cap: int) -> list[str]:
    """Strip whitespace, drop empty entries, then truncate to cap.

    Order matters: dropping empties BEFORE truncation means the cap counts
    real content. Otherwise `["", "", "real reason"]` truncated to 5 keeps
    the two leading empties.
    """
    cleaned = [s.strip() for s in items]
    nonempty = [s for s in cleaned if s]
    return nonempty[:cap]


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
