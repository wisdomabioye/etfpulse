"""Confidence calibration — empirical reliability curve.

Answers "does confidence=8 actually win 80% of the time?" with real data.
On-read aggregation (no stored table). The route caches the result for a few
minutes; daily snapshot persistence is intentionally deferred unless we
actually need historical reliability tracking (PR I.1 design decision).

Public surface:
    `compute_calibration(session, *, ai_prompt_version, lookback_days,
                          min_samples, bucket_size) -> CalibrationReport`
        D14 — does NOT commit; caller owns the session (always read-only
        for this module, but the contract is uniform).

Pure helpers (testable without a DB):
    `make_buckets(*, bucket_size)`        — confidence 1..10 → bucket ranges
    `classify_outcome_as_win(outcome)`    — single source of truth for win/loss

Wilson 95% CI lives in `pipeline.stats` (shared with `pipeline.per_detector`
so neither aggregator depends on the other).

What counts (filters):
    - `evaluated_outcomes_predicate()` from `pipeline.track_record`
      (shared with calibration / per-floor stats / recent outcomes / route)
    - `Signal.ai_prompt_version == <param>`  (compare apples-to-apples
      across prompt revisions — see analysis.AI_PROMPT_VERSION docstring)
    - `SignalOutcome.evaluated_at >= now - lookback_days`
    - `SignalOutcome.hit_target IS NOT NULL`  (no-target outcomes have no
      win/loss to count — same `targeted_count` convention as the route)

Bucket-by-horizon shape:
    Confidence bucketed via `make_buckets(bucket_size=2)` by default →
    5 buckets {1-2, 3-4, 5-6, 7-8, 9-10}. Each bucket × {scalp, swing,
    position, legacy} = 20 cells. Most cells will be sparse early on
    (especially scalp before intraday klines (#62) land); the
    `min_samples` gate marks them as `hit_rate=None` so the FE renders
    "—" rather than a misleading point estimate.

Why Wilson (not normal-approximation):
    At small N the normal-approximation CI is famously bad — for n=10
    wins=10 it reports [1.0, 1.0]. Wilson handles boundary cases cleanly.
    Standard formula; hand-rolled here rather than pulling scipy (which
    isn't a dep) — tested against known-good values.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from etfpulse.models import Signal, SignalOutcome
from etfpulse.pipeline.stats import wilson_ci
from etfpulse.pipeline.track_record import (
    HORIZON_LABELS,
    HorizonLabel,
    evaluated_outcomes_predicate,
    horizon_label_for,
)

log = structlog.get_logger()


# Only divisors of 10 — anything else can't partition the 1..10 confidence
# range into equal buckets without a remainder.
_VALID_BUCKET_SIZES: frozenset[int] = frozenset({1, 2, 5, 10})


@dataclass(frozen=True, slots=True)
class CalibrationBucket:
    """One (confidence-bucket × horizon) cell of the reliability surface.

    `hit_rate`, `ci_low`, `ci_high` are None when `n_samples < min_samples`
    OR when `n_samples == 0` (cold-start). The dataclass shape is stable so
    the FE always sees the full grid — empty cells render as "—" rather
    than disappearing.
    """

    bucket_floor: int  # inclusive [1..10]
    bucket_ceiling: int  # inclusive [1..10] (>= bucket_floor)
    horizon: HorizonLabel
    n_samples: int  # wins + losses (excludes hit_target IS NULL)
    wins: int
    losses: int
    hit_rate: float | None  # 0..1; None when n < min_samples
    ci_low: float | None  # 0..1
    ci_high: float | None  # 0..1


@dataclass(frozen=True, slots=True)
class CalibrationReport:
    """Full reliability surface for one (prompt_version, lookback) cohort."""

    ai_prompt_version: str
    lookback_days: int
    min_samples: int
    bucket_size: int
    buckets: list[CalibrationBucket]


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def make_buckets(*, bucket_size: int = 2) -> list[tuple[int, int]]:
    """Carve confidence 1..10 into consecutive inclusive (floor, ceiling) ranges.

    `bucket_size=2` (default) → `[(1,2), (3,4), (5,6), (7,8), (9,10)]`.
    Five buckets is the sweet spot at our N — small enough that most
    buckets have data, big enough to surface a calibration story.

    `bucket_size` MUST divide 10 evenly (1, 2, 5, or 10). Other values
    would leave a partial trailing bucket which would skew the rendering
    and force special-case downstream code.
    """
    if bucket_size not in _VALID_BUCKET_SIZES:
        raise ValueError(
            f"bucket_size must be one of {sorted(_VALID_BUCKET_SIZES)}, got {bucket_size}"
        )
    return [(lo, lo + bucket_size - 1) for lo in range(1, 11, bucket_size)]


def classify_outcome_as_win(outcome: SignalOutcome) -> bool | None:
    """Win/loss definition for calibration — single source of truth.

    Returns:
        True  — signal hit its target (counts as 1 win, 1 sample).
        False — signal didn't hit target but had one (counts as 1 loss,
                 1 sample). Includes signals that hit their stop.
        None  — AI declined to set a target. EXCLUDED from numerator AND
                 denominator. Same convention as the public hit-rate.

    Centralising this means a future "what counts as a win" change (e.g.
    require `max_favorable > target_distance` instead of strict hit) lands
    in one place and propagates to calibration plus the public hit-rate
    surface together.
    """
    return outcome.hit_target


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


async def compute_calibration(
    session: AsyncSession,
    *,
    ai_prompt_version: str,
    lookback_days: int,
    min_samples: int,
    bucket_size: int = 2,
) -> CalibrationReport:
    """Aggregate evaluated outcomes into a (bucket × horizon) reliability grid.

    D14 — does NOT commit. Read-only against `signal_outcomes` JOIN `signals`.

    Filters applied:
        - `evaluated_outcomes_predicate()`  (shared with track_record)
        - `Signal.ai_prompt_version == <param>`
        - `SignalOutcome.evaluated_at >= now - lookback_days`
        - `SignalOutcome.hit_target IS NOT NULL`  (no-target rows excluded)

    Returns a `CalibrationReport` with one entry per (bucket × horizon)
    combination — buckets with no data report n=0 + hit_rate=None.

    Logs `calibration_computed` once per call. Includes an
    `insufficient_buckets` list of cells where `0 < n < min_samples` so
    operators see which cohort needs more data (without 20 separate log
    lines on a cold-start day).
    """
    cutoff = datetime.now(UTC) - timedelta(days=lookback_days)
    bucket_ranges = make_buckets(bucket_size=bucket_size)

    stmt = (
        select(
            SignalOutcome.confidence,
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
        .group_by(SignalOutcome.confidence, SignalOutcome.window_hours)
    )
    rows = (await session.execute(stmt)).all()

    # Build a (confidence -> bucket_range) lookup so the Python aggregation
    # is O(rows) not O(rows × buckets).
    conf_to_bucket: dict[int, tuple[int, int]] = {}
    for floor, ceiling in bucket_ranges:
        for c in range(floor, ceiling + 1):
            conf_to_bucket[c] = (floor, ceiling)

    # Aggregate raw (confidence, window_hours) rows into (bucket, horizon) cells.
    # Multiple distinct window_hours values can map to the same horizon label
    # (e.g. 168h and 240h both → "position") so we accumulate.
    #
    # `conf_to_bucket[row.confidence]` is a direct lookup — no defensive
    # branch needed because `SignalOutcome.confidence` has a CHECK constraint
    # enforcing 1..10 (see `models/signal.py:170-171`), and `make_buckets`
    # covers every value in that range by construction.
    per_cell: dict[tuple[int, int, HorizonLabel], tuple[int, int]] = {}
    for row in rows:
        bucket = conf_to_bucket[row.confidence]
        horizon = horizon_label_for(row.window_hours)
        key = (bucket[0], bucket[1], horizon)
        prior_w, prior_l = per_cell.get(key, (0, 0))
        per_cell[key] = (prior_w + row.wins, prior_l + row.losses)

    # Materialize the full grid so the response shape is stable. Empty cells
    # get n=0, hit_rate=None — the FE renders them as "—" placeholders.
    buckets_out: list[CalibrationBucket] = []
    insufficient: list[tuple[int, int, HorizonLabel, int]] = []
    for floor, ceiling in bucket_ranges:
        for horizon in HORIZON_LABELS:
            wins, losses = per_cell.get((floor, ceiling, horizon), (0, 0))
            n = wins + losses
            if n >= min_samples:
                hit_rate: float | None = wins / n
                ci_low, ci_high = wilson_ci(wins, n)
            else:
                hit_rate = None
                ci_low, ci_high = None, None
                if n > 0:
                    insufficient.append((floor, ceiling, horizon, n))
            buckets_out.append(
                CalibrationBucket(
                    bucket_floor=floor,
                    bucket_ceiling=ceiling,
                    horizon=horizon,
                    n_samples=n,
                    wins=wins,
                    losses=losses,
                    hit_rate=hit_rate,
                    ci_low=ci_low,
                    ci_high=ci_high,
                )
            )

    # One structured log line per request — includes a list of insufficient
    # buckets so the operator can see "swing 5-6 has n=4, needs 20" without
    # us emitting 20 separate log lines on a cold cohort. Empty list when
    # every bucket is either above min_samples OR has zero samples.
    total_n = sum(b.n_samples for b in buckets_out)
    n_above_min = sum(1 for b in buckets_out if b.n_samples >= min_samples)
    log.info(
        "calibration_computed",
        ai_prompt_version=ai_prompt_version,
        lookback_days=lookback_days,
        min_samples=min_samples,
        bucket_size=bucket_size,
        n_total=total_n,
        buckets_total=len(buckets_out),
        buckets_above_min_samples=n_above_min,
        insufficient_buckets=[
            {"floor": f, "ceiling": c, "horizon": h, "n": n} for f, c, h, n in insufficient
        ],
    )

    return CalibrationReport(
        ai_prompt_version=ai_prompt_version,
        lookback_days=lookback_days,
        min_samples=min_samples,
        bucket_size=bucket_size,
        buckets=buckets_out,
    )


__all__ = [
    "CalibrationBucket",
    "CalibrationReport",
    "classify_outcome_as_win",
    "compute_calibration",
    "make_buckets",
]
