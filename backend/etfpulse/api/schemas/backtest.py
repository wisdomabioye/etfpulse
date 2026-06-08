"""Response shapes for `POST /api/admin/backtest` (PR P2.4).

Mirrors the dataclasses in `pipeline.backtest` so a JSON-serialized
`BacktestReport.to_json_dict()` validates cleanly against the response
schema and the FE has a typed contract. The dataclasses stay the source
of truth for what the pipeline produces; this module is the on-the-wire
translation only.

Why scalar-union instead of `Any` for `detector_configs` and
`detector_overrides`:
  Detector constructor kwargs are heterogeneous SCALARS (int, float,
  bool, Decimal-as-string) — never nested objects. Constraining the
  union to those primitives keeps Pydantic validation tight and avoids
  the project's "no `Any` unless absolutely necessary" discipline.
  `Decimal` values arrive as strings (see `_build_detector` in
  `pipeline/backtest.py`) so `str` covers them losslessly.

Why `start`/`end` are strings (not `date`):
  The pipeline dataclass already serializes them as ISO strings (line
  144 of `pipeline/backtest.py`). Mirroring the wire shape keeps the
  schema lossless against `BacktestReport.to_json_dict()`. The request
  schema (`BacktestRequest`) uses `date` because Pydantic auto-parses
  ISO input there; route logic does the date→str conversion before
  calling the orchestrator.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# Allowed kwarg value types for detector overrides. Mirrors the
# heterogeneous-scalar reality of detector constructor signatures.
# Pydantic's union resolution will coerce in-order; bool BEFORE int so
# `True` doesn't deserialize as `1`.
DetectorKwargValue = bool | int | float | str

# Literal mirroring `pipeline.backtest.BacktestOutcomeRow.scoring_version`.
ScoringVersion = Literal["v2", "market-v1"]


class BacktestRequest(BaseModel):
    """Body shape for `POST /api/admin/backtest`.

    Window-size cap and detector-override validation happen at the
    ROUTE layer (#201). This schema just pins shape + parses dates.
    """

    model_config = ConfigDict(extra="forbid")

    start: date
    end: date
    detector_overrides: dict[str, dict[str, DetectorKwargValue]] | None = None
    allow_ai: bool = False


class BacktestOutcomeRowOut(BaseModel):
    """One row in `BacktestReport.outcomes`. Mirrors
    `pipeline.backtest.BacktestOutcomeRow` field-for-field."""

    model_config = ConfigDict(extra="forbid")

    detector_name: str
    signal_type: str
    asset: str
    signal_date: str  # ISO date — pipeline serializes as str
    fingerprint: str
    direction: str | None
    confidence: int | None = Field(default=None, ge=1, le=10)
    hit_target: bool | None
    hit_stop: bool | None
    composite_return_pct: str | None  # str(Decimal) for lossless JSON
    scoring_version: ScoringVersion | None
    window_hours: int | None = Field(default=None, ge=0)
    skip_reason: str | None


class BacktestPerDetectorOut(BaseModel):
    """One row in `BacktestReport.per_detector`. Mirrors
    `pipeline.backtest.BacktestPerDetector`."""

    model_config = ConfigDict(extra="forbid")

    detector_name: str
    n_hits: int = Field(ge=0)
    n_scored: int = Field(ge=0)
    wins: int = Field(ge=0)
    losses: int = Field(ge=0)
    hit_rate: float | None = Field(default=None, ge=0.0, le=1.0)


class BacktestReportOut(BaseModel):
    """Response shape for `POST /api/admin/backtest`. Mirrors
    `pipeline.backtest.BacktestReport.to_json_dict()` exactly."""

    model_config = ConfigDict(extra="forbid")

    start: str  # ISO date
    end: str  # ISO date
    ai_prompt_version: str
    detector_configs: dict[str, dict[str, DetectorKwargValue]]
    counters: dict[str, int]
    per_detector: list[BacktestPerDetectorOut]
    outcomes: list[BacktestOutcomeRowOut]


class BacktestDetectorParamOut(BaseModel):
    """Single constructor kwarg signature for the detector listing route
    (#202). FE form renders one input per parameter."""

    model_config = ConfigDict(extra="forbid")

    name: str
    # Annotation type as a human-readable string ("int", "float", "Decimal",
    # "bool"). FE picks input widget based on this. Decimal renders as a
    # numeric text input (no native decimal type in browsers).
    type_name: str
    has_default: bool
    default: DetectorKwargValue | None = None


class BacktestDetectorOut(BaseModel):
    """One detector entry for `GET /api/admin/backtest/detectors`."""

    model_config = ConfigDict(extra="forbid")

    name: str
    signal_type: str
    params: list[BacktestDetectorParamOut]


class BacktestDetectorsResponse(BaseModel):
    """Response shape for `GET /api/admin/backtest/detectors`."""

    model_config = ConfigDict(extra="forbid")

    detectors: list[BacktestDetectorOut]
