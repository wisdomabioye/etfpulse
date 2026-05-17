"""Response shapes for `GET /api/track-record/per-detector`.

PR I.3 — per-detector precision grid. Mirrors the internal
`PerDetectorReport` / `DetectorRow` / `DetectorHorizonCell` dataclasses
from `pipeline.per_detector`; the API layer's job is the JSON contract only.

Grid shape is ALWAYS fully populated: every detector × every horizon is
present, with `hit_rate=null` / `ci_*=null` for cells below min_samples or
with no data. The `total` cell summarises across horizons and is the
primary FE rendering surface today (Option C "leaderboard" layout) — the
per-horizon cells are kept in the same response so future drill-downs
don't require an API change.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from etfpulse.api.schemas.track_record import HorizonLiteral


class DetectorHorizonCellOut(BaseModel):
    """One (detector × horizon) cell, OR the across-horizons total cell.

    `hit_rate`, `ci_low`, `ci_high` are nullable because:
      - `n_samples == 0` (cold start / detector hasn't fired yet): no
        proportion to report.
      - `n_samples < min_samples` (insufficient signal): point estimate
        would be noisy; FE renders "—" until N grows.
    """

    # `from_attributes=True` so the route can call
    # `PerDetectorResponse.model_validate(internal_report)` and Pydantic
    # recurses into the nested dataclasses + dict-of-dataclass values.
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    n_samples: int = Field(ge=0)
    wins: int = Field(ge=0)
    losses: int = Field(ge=0)
    hit_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    ci_low: float | None = Field(default=None, ge=0.0, le=1.0)
    ci_high: float | None = Field(default=None, ge=0.0, le=1.0)


class DetectorRowOut(BaseModel):
    """One detector's slice — per-horizon cells + the across-horizons total.

    `signal_type` is a free `str` rather than a strict Literal: legacy /
    removed detectors with historical data still surface here, and a
    future detector lands as a new value without an API contract change.
    Validation of "is this a known detector?" is a FE concern (labelling /
    sorting), not a route contract.
    """

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    signal_type: str = Field(min_length=1)
    horizons: dict[HorizonLiteral, DetectorHorizonCellOut]
    total: DetectorHorizonCellOut


class PerDetectorResponse(BaseModel):
    """Full per-detector precision report for one (prompt_version, lookback) cohort.

    On cold start / unseeded DB: `detectors` still lists every registered
    detector with all-zero cells, so the FE has stable rows to render.
    regime_shift is omitted by design (PR I.3b will fold it in once
    composite scoring lands).
    """

    # `from_attributes=True` so the route calls `model_validate(internal_report)`
    # once and Pydantic recurses through the nested dataclass + dict-of-cells.
    # No hand-rolled field-by-field copy = no drift when a new field lands.
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    ai_prompt_version: str = Field(pattern=r"^v[0-9]+$")
    lookback_days: int = Field(ge=1)
    min_samples: int = Field(ge=1)
    detectors: list[DetectorRowOut]
