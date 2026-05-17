"""Per-detector precision aggregator — PR I.3.

Answers "which detector is actually predictive?" by grouping evaluated
outcomes by `Signal.signal_type` and the horizon bucket each outcome was
scored against. On-read, no stored snapshot (same pattern as
`pipeline.calibration`).

Public surface:
    `compute_per_detector(session, *, ai_prompt_version, lookback_days,
                           min_samples) -> PerDetectorReport`
        D14 — does NOT commit (read-only). Caller owns the session.

What counts (filter chain, single source of truth):
    - `evaluated_outcomes_predicate()` from `pipeline.track_record`
      (`evaluated_at IS NOT NULL`). Third consumer after calibration and
      per-confidence-floor stats — reinforces D22.
    - `Signal.ai_prompt_version == <param>` so a prompt revision doesn't
      contaminate the cohort with apples-to-oranges scoring.
    - `SignalOutcome.evaluated_at >= now - lookback_days` — rolling window.
    - `SignalOutcome.hit_target IS NOT NULL` — no-target rows are
      uncountable.

Detector ordering (stable across calls):
    1. `ALL_DETECTORS` (registered detector list) minus the excluded set
       — currently `{"regime_shift"}`. MARKET-asset signals can't be
       scored against single-asset entry/stop/target levels; PR I.3b
       (composite scoring) will lift the exclusion in one line by emptying
       `_EXCLUDED_FROM_PER_DETECTOR`.
    2. Any `signal_type` value found in DB results that ISN'T in
       `ALL_DETECTORS` (legacy / removed detectors with historical data)
       — appended alphabetically at the tail.

Total cell semantics:
    Each `DetectorRow` carries a `total` `DetectorHorizonCell` summing
    wins/losses across all 4 horizons for that detector. min_samples is
    applied independently to each per-horizon cell AND to the total —
    so a detector with n=10 total split unevenly (6/3/1/0 across
    horizons) shows `total.hit_rate` populated while every horizon cell
    stays "—" at min_samples=5. The FE renders both modes from one payload.

Why a separate module (not folded into `track_record.py`):
    Same separation as PR I.1's `calibration.py` — read-only aggregators
    sibling-by-sibling, `track_record.py` stays the outcome-evaluator home.
    Both aggregators import shared helpers from `track_record` (predicate,
    horizon bucketing) — dependency direction stays one-way.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from etfpulse.models import Signal, SignalOutcome
from etfpulse.pipeline.detectors import ALL_DETECTORS
from etfpulse.pipeline.stats import wilson_ci
from etfpulse.pipeline.track_record import (
    HORIZON_LABELS,
    HorizonLabel,
    evaluated_outcomes_predicate,
    horizon_label_for,
)

log = structlog.get_logger()


# Detectors whose outcomes are NOT scored under any rubric this report
# covers. Historically (pre-PR-I.3b) regime_shift lived here because MARKET
# signals were unscored — they had no single-asset entry/stop/target so the
# outcome evaluator skipped them. PR I.3b added composite BTC+ETH scoring
# for MARKET (`scoring_version='market-v1'`, hit_target derived from a
# weighted composite return), which means regime_shift outcomes NOW have
# valid `hit_target` bits and fold into the per-detector report on the
# same footing as single-asset detectors. The set is empty today but kept
# as the documented seam — a future "unscorable" detector category lands
# here without touching the aggregation loop.
_EXCLUDED_FROM_PER_DETECTOR: frozenset[str] = frozenset()


# ---------------------------------------------------------------------------
# Internal dataclasses (returned to the route, which maps to Pydantic Out)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DetectorHorizonCell:
    """One (detector × horizon) cell of the precision grid.

    `hit_rate` / `ci_low` / `ci_high` are None when `n_samples < min_samples`
    OR when `n_samples == 0`. The dataclass shape is stable so the FE always
    sees every cell — empty cells render as "—" rather than disappearing.
    """

    n_samples: int  # wins + losses (hit_target IS NOT NULL only)
    wins: int
    losses: int
    hit_rate: float | None  # 0..1
    ci_low: float | None  # 0..1
    ci_high: float | None  # 0..1


@dataclass(frozen=True, slots=True)
class DetectorRow:
    """One detector's slice — per-horizon cells plus the across-horizons total.

    `total` is pre-computed by the backend (rather than asked of the FE)
    so the API contract makes the leaderboard rendering explicit. Same
    min_samples gate applies to `total` as to the horizon cells; the
    threshold being identical means a 4× looser bar than rendering only
    when every horizon cell is above min_samples.
    """

    signal_type: str
    horizons: dict[HorizonLabel, DetectorHorizonCell]
    total: DetectorHorizonCell


@dataclass(frozen=True, slots=True)
class PerDetectorReport:
    """Top-level response. `detectors` is ordered (see module docstring)."""

    ai_prompt_version: str
    lookback_days: int
    min_samples: int
    detectors: list[DetectorRow]


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


async def compute_per_detector(
    session: AsyncSession,
    *,
    ai_prompt_version: str,
    lookback_days: int,
    min_samples: int,
) -> PerDetectorReport:
    """Aggregate evaluated outcomes into a (detector × horizon) grid.

    D14 — does NOT commit; pure read on `signal_outcomes JOIN signals`.

    One GROUP BY query, post-aggregation in Python (≤ ~30 rows even at
    100k signals — multiple `window_hours` values mapping to the same
    horizon label get summed in the loop, mirroring calibration's pattern).
    """
    cutoff = datetime.now(UTC) - timedelta(days=lookback_days)

    stmt = (
        select(
            Signal.signal_type,
            SignalOutcome.window_hours,
            func.count().filter(SignalOutcome.hit_target.is_(True)).label("wins"),
            func.count().filter(SignalOutcome.hit_target.is_(False)).label("losses"),
        )
        .select_from(SignalOutcome)
        .join(Signal, Signal.id == SignalOutcome.signal_id)
        .where(
            evaluated_outcomes_predicate(),
            Signal.ai_prompt_version == ai_prompt_version,
            SignalOutcome.evaluated_at >= cutoff,
            SignalOutcome.hit_target.is_not(None),
        )
        .group_by(Signal.signal_type, SignalOutcome.window_hours)
    )
    rows = (await session.execute(stmt)).all()

    # Aggregate raw (signal_type, window_hours) rows into (signal_type,
    # horizon) cells. Multiple window_hours values can map to one label
    # (e.g. 168h and 240h both → "position") so accumulate.
    per_cell: dict[tuple[str, HorizonLabel], tuple[int, int]] = {}
    seen_signal_types: set[str] = set()
    for row in rows:
        seen_signal_types.add(row.signal_type)
        horizon = horizon_label_for(row.window_hours)
        key = (row.signal_type, horizon)
        prior_w, prior_l = per_cell.get(key, (0, 0))
        per_cell[key] = (prior_w + row.wins, prior_l + row.losses)

    ordered_signal_types = _ordered_detector_list(seen_signal_types)

    rows_out: list[DetectorRow] = []
    for signal_type in ordered_signal_types:
        horizons: dict[HorizonLabel, DetectorHorizonCell] = {}
        total_wins = 0
        total_losses = 0
        for horizon in HORIZON_LABELS:
            wins, losses = per_cell.get((signal_type, horizon), (0, 0))
            horizons[horizon] = _build_cell(wins, losses, min_samples=min_samples)
            total_wins += wins
            total_losses += losses
        total_cell = _build_cell(total_wins, total_losses, min_samples=min_samples)
        rows_out.append(DetectorRow(signal_type=signal_type, horizons=horizons, total=total_cell))

    log.info(
        "per_detector_computed",
        ai_prompt_version=ai_prompt_version,
        lookback_days=lookback_days,
        min_samples=min_samples,
        detectors_returned=len(rows_out),
        n_total=sum(r.total.n_samples for r in rows_out),
        n_above_min_samples_total=sum(1 for r in rows_out if r.total.hit_rate is not None),
    )

    return PerDetectorReport(
        ai_prompt_version=ai_prompt_version,
        lookback_days=lookback_days,
        min_samples=min_samples,
        detectors=rows_out,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _ordered_detector_list(seen_in_data: set[str]) -> list[str]:
    """Build the ordered list of signal_types to emit.

    Order:
        1. `ALL_DETECTORS` precedence order, minus `_EXCLUDED_FROM_PER_DETECTOR`,
           DEDUPED on first occurrence. Registered detectors ALWAYS appear —
           even with zero data — so the FE has stable rows on cold-start and
           after a new detector lands but before its first signal evaluates.
        2. Any `signal_type` found in DB results that isn't registered
           (legacy/removed detectors with historical data), sorted
           alphabetically for stability. Also filtered by
           `_EXCLUDED_FROM_PER_DETECTOR` so a regime_shift signal that
           somehow snuck into the outcomes table (it shouldn't — see
           module docstring) still doesn't surface here.

    Defensive dedupe: `tests/test_pipeline/test_detectors_framework.py`
    pins `detector.name` uniqueness across `ALL_DETECTORS` but does NOT
    pin `detector.signal_type` uniqueness — the Protocol allows two
    detectors to share a `signal_type` while having distinct `name`s
    (e.g. an A/B-tested "acceleration_v2" emitting `signal_type="acceleration"`).
    Without dedupe the FE would render two identical rows and the
    `dict[HorizonLabel, …]` consumers would collide on the second pass.
    """
    registered: list[str] = []
    seen_registered: set[str] = set()
    for detector in ALL_DETECTORS:
        st = detector.signal_type
        if st in _EXCLUDED_FROM_PER_DETECTOR or st in seen_registered:
            continue
        seen_registered.add(st)
        registered.append(st)
    legacy = sorted(seen_in_data - seen_registered - _EXCLUDED_FROM_PER_DETECTOR)
    return registered + legacy


def _build_cell(wins: int, losses: int, *, min_samples: int) -> DetectorHorizonCell:
    """Materialize one cell. `hit_rate` only populated when n is positive
    AND meets min_samples — small-N cells render as "—" on the FE."""
    n = wins + losses
    if n >= min_samples and n > 0:
        hit_rate: float | None = wins / n
        ci_low, ci_high = wilson_ci(wins, n)
    else:
        hit_rate = None
        ci_low, ci_high = None, None
    return DetectorHorizonCell(
        n_samples=n,
        wins=wins,
        losses=losses,
        hit_rate=hit_rate,
        ci_low=ci_low,
        ci_high=ci_high,
    )


__all__ = [
    "DetectorHorizonCell",
    "DetectorRow",
    "PerDetectorReport",
    "compute_per_detector",
]
