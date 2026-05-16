"""Response shapes for `GET /api/track-record/calibration`.

PR I.1 — reliability curve per (confidence bucket × horizon). Mirrors the
internal `CalibrationReport` / `CalibrationBucket` dataclasses from
`pipeline.calibration`; the API layer's job is the JSON contract only.

Bucket grid is ALWAYS fully populated — every (bucket × horizon)
combination is present, with `hit_rate=null` / `ci_*=null` for empty or
under-min-samples cells. Stable shape across deploys = FE renders the
grid as fixed-position tiles, no layout shift when data arrives.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from etfpulse.api.schemas.track_record import HorizonLiteral


class CalibrationBucketOut(BaseModel):
    """One cell of the reliability grid.

    `hit_rate`, `ci_low`, `ci_high` are nullable because:
      - `n_samples == 0` (cold start / sparse cohort): no proportion to report.
      - `n_samples < min_samples` (insufficient signal): point estimate would
        be noisy enough to mislead; the FE renders "—" until N grows.

    `bucket_floor` / `bucket_ceiling` are inclusive [1..10]. With the default
    `bucket_size=2`, ranges are (1,2), (3,4), (5,6), (7,8), (9,10).
    """

    # `from_attributes=True` lets the route construct via
    # `CalibrationBucketOut.model_validate(internal_bucket)` straight off
    # the `pipeline.calibration.CalibrationBucket` dataclass — no hand-
    # rolled field-by-field copy that could drift if a field is added.
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    bucket_floor: int = Field(ge=1, le=10)
    bucket_ceiling: int = Field(ge=1, le=10)
    horizon: HorizonLiteral
    n_samples: int = Field(ge=0)
    wins: int = Field(ge=0)
    losses: int = Field(ge=0)
    hit_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    ci_low: float | None = Field(default=None, ge=0.0, le=1.0)
    ci_high: float | None = Field(default=None, ge=0.0, le=1.0)


class CalibrationResponse(BaseModel):
    """Full reliability surface for one (prompt_version, lookback) cohort.

    On cold start / unseeded DB: `buckets` is still the full grid (20 cells
    by default) with every `n_samples=0`. Frontend renders the empty grid
    so the layout is consistent with the populated state — no "no data"
    short-circuit.
    """

    model_config = ConfigDict(extra="forbid")

    ai_prompt_version: str = Field(pattern=r"^v[0-9]+$")
    lookback_days: int = Field(ge=1)
    min_samples: int = Field(ge=1)
    bucket_size: int = Field(ge=1, le=10)
    buckets: list[CalibrationBucketOut]
