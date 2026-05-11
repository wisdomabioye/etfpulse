"""Track-record diagnostic breakdown — Stage 8-P10.

Public surface:
    `get_track_record_breakdown(session) -> TrackRecordBreakdown`
        One-shot aggregator that produces every diagnostic slice the
        `/analytics` public page renders. Caller owns the transaction
        (D14) — same contract as `pipeline.track_record`.

Diagnostic dimensions (4 breakdowns + 2 histograms):

    1. **Detector** (signal_type)
       Question: *Which of the 5 detectors earn their compute?*
       Action: if a detector's hit rate trails the others by a wide margin,
       tune its thresholds (env vars per CLAUDE.md) or drop it.

    2. **Asset** (BTC vs ETH)
       Question: *Is one asset systematically easier than the other?*
       Action: sanity check — large skew suggests data quality issue or
       per-asset prompt tuning.

    3. **Confidence bucket** (1–3 / 4–6 / 7–8 / 9–10)
       Question: *Is the AI confidence score calibrated?*
       Action: per-bucket (NOT cumulative) is the right view for
       calibration. `get_stats_by_confidence_floor` shows cumulative and
       therefore blurs the high-tail signal — `floor=7` includes 7+8+9+10.
       Per-bucket reveals whether 9–10 actually beats 7–8.

    4. **Direction** (long / short / neutral)
       Question: *Is there LLM bull bias?* (Critical for prompt-driven
       signal systems — known failure mode.)
       Action: if `long` overwhelms with mediocre hit rate while `short` is
       rare-but-accurate, the prompt or the regime gate needs to push back
       on long-bias.

    5. **MFE histogram** (max_favorable distribution)
       Question: *For signals that missed target, how close did they get?*
       Action: if most got 80% of the way, targets are too aggressive. If
       most barely moved, entry prices are bad.

    6. **MAE histogram** (max_adverse distribution)
       Question: *For signals that didn't hit stop, how close did they get?*
       Action: lots of near-stops = stops too tight; flat distribution =
       stops too loose / not protective.

Implementation notes:

    * Every query filters `evaluated_at IS NOT NULL` (same defensive filter
      as `routes/track_record.py` + `routes/dashboard.py`) — pending-eval
      rows must not pollute the breakdown.

    * Five GROUP BY queries (one per breakdown, plus one for histograms via
      raw value fetch) + zero auxiliary COUNTs — `total_outcomes` is derived
      from the detector breakdown sum since every outcome has a signal_type.

    * Histogram bucketing runs in Python over the raw fetched values, NOT
      via Postgres `width_bucket()` — keeps the SQL trivially auditable and
      makes the bucket edges live as a single Python constant that's easy
      to revise. Volumes are small (hundreds of rows today, thousands at
      worst); this is comfortably under the threshold where SQL bucketing
      would matter.

    * Returns plain frozen dataclasses, NOT Pydantic — the pipeline layer
      stays HTTP-agnostic per the CLAUDE.md domain/api separation rule.
      The route converts to the response schema in `api/routes/analytics.py`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal

import structlog
from cachetools import TTLCache
from sqlalchemy import Numeric, case, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from etfpulse.models import SignalOutcome
from etfpulse.pipeline.track_record import compute_hit_rate_pct

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Constants — bucket edges live HERE so revisions are one-file changes.
# ---------------------------------------------------------------------------


# Confidence buckets — 4 bands that separate the upper tail from "merely
# confident". Order is bottom-up so the rendered chart reads naturally.
_CONFIDENCE_BUCKET_LOW = "1–3 (low)"
_CONFIDENCE_BUCKET_MID = "4–6 (mid)"
_CONFIDENCE_BUCKET_HIGH = "7–8 (high)"
_CONFIDENCE_BUCKET_VERY_HIGH = "9–10 (very high)"

_CONFIDENCE_BUCKET_ORDER: tuple[str, ...] = (
    _CONFIDENCE_BUCKET_LOW,
    _CONFIDENCE_BUCKET_MID,
    _CONFIDENCE_BUCKET_HIGH,
    _CONFIDENCE_BUCKET_VERY_HIGH,
)


# MFE/MAE histogram edges — fixed, log-ish spacing. Stable across weeks so
# cross-period comparison stays meaningful. Edges are LEFT-CLOSED,
# RIGHT-OPEN; the final bucket is open-ended (≥10%). max_favorable and
# max_adverse are stored as unsigned fractions (Decimal), e.g. 0.025 = 2.5%.
_HISTOGRAM_EDGES: tuple[float, ...] = (0.0, 0.005, 0.01, 0.02, 0.05, 0.10, math.inf)
_HISTOGRAM_LABELS: tuple[str, ...] = (
    "<0.5%",
    "0.5–1%",
    "1–2%",
    "2–5%",
    "5–10%",
    "≥10%",
)
assert len(_HISTOGRAM_LABELS) == len(_HISTOGRAM_EDGES) - 1, (
    "label count must equal edge count minus 1"
)


# ---------------------------------------------------------------------------
# DTOs — plain frozen dataclasses. Route layer projects these to Pydantic.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BreakdownRow:
    """One category in a categorical breakdown (detector / asset / direction
    / confidence-bucket).

    `total` counts evaluated rows in this category; `targeted` is the subset
    where the signal had a target_price (denominator for hit_rate); `hits` is
    the further subset where `hit_target=True`. `hit_rate_pct` is `None` when
    `targeted == 0` (same null-vs-zero convention as `compute_hit_rate_pct`).
    """

    label: str
    total: int
    targeted: int
    hits: int
    hit_rate_pct: float | None


@dataclass(frozen=True, slots=True)
class HistogramBucket:
    """One bin of the MFE/MAE histogram.

    `lower` is inclusive, `upper` is exclusive. The final bucket has
    `upper = None` indicating open-ended (≥10%). `count` is the number of
    outcomes whose value fell into [lower, upper).
    """

    label: str
    lower: float
    upper: float | None
    count: int


@dataclass(frozen=True, slots=True)
class TrackRecordBreakdown:
    """Full diagnostic surface for `/analytics`.

    `total_outcomes` is the global denominator for cohort-size captions
    — derived from the detector breakdown sum since every outcome has a
    signal_type.
    """

    total_outcomes: int
    by_detector: list[BreakdownRow]
    by_asset: list[BreakdownRow]
    by_confidence_bucket: list[BreakdownRow]
    by_direction: list[BreakdownRow]
    mfe_histogram: list[HistogramBucket]
    mae_histogram: list[HistogramBucket]


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _bucket_index(value: float) -> int:
    """Return the histogram bucket index for `value` (0..len-1).

    Edges are LEFT-CLOSED, RIGHT-OPEN. The final bucket absorbs everything
    ≥ the last finite edge (math.inf upper). Negative values (shouldn't
    happen — MFE/MAE are unsigned) land in bucket 0 rather than raising.
    """
    for i in range(len(_HISTOGRAM_LABELS)):
        if value < _HISTOGRAM_EDGES[i + 1]:
            return i
    # Unreachable in practice — math.inf is the last upper edge — but
    # safer than IndexError if the edge tuple is ever mis-edited.
    return len(_HISTOGRAM_LABELS) - 1


def _build_histogram(values: list[Decimal]) -> list[HistogramBucket]:
    """Bucket a list of unsigned-fraction values into the fixed histogram.

    Empty input returns the full bucket list with `count=0` — preserves
    the chart's structure on cold-boot, so the FE renders an empty axis
    instead of a missing section.
    """
    counts = [0] * len(_HISTOGRAM_LABELS)
    for v in values:
        idx = _bucket_index(float(v))
        counts[idx] += 1

    return [
        HistogramBucket(
            label=_HISTOGRAM_LABELS[i],
            lower=_HISTOGRAM_EDGES[i],
            upper=None if math.isinf(_HISTOGRAM_EDGES[i + 1]) else _HISTOGRAM_EDGES[i + 1],
            count=counts[i],
        )
        for i in range(len(_HISTOGRAM_LABELS))
    ]


def _build_row(label: str, total: int, targeted: int, hits: int) -> BreakdownRow:
    """Pack a GROUP BY row into a BreakdownRow. Centralises the
    hit-rate-via-shared-helper call so every breakdown computes it the
    same way (single source of truth for the percent math)."""
    return BreakdownRow(
        label=label,
        total=total,
        targeted=targeted,
        hits=hits,
        hit_rate_pct=compute_hit_rate_pct(hits, targeted),
    )


# ---------------------------------------------------------------------------
# Per-dimension query functions — each runs ONE GROUP BY against
# `signal_outcomes` filtered to `evaluated_at IS NOT NULL`. Split out for
# readability + per-dimension testability; composed in
# `get_track_record_breakdown` below.
# ---------------------------------------------------------------------------


_TARGETED = func.count().filter(SignalOutcome.hit_target.is_not(None))
_HITS = func.count().filter(SignalOutcome.hit_target.is_(True))


async def _by_detector(session: AsyncSession) -> list[BreakdownRow]:
    stmt = (
        select(
            SignalOutcome.signal_type.label("category"),
            func.count().label("total"),
            _TARGETED.label("targeted"),
            _HITS.label("hits"),
        )
        .where(SignalOutcome.evaluated_at.is_not(None))
        .group_by(SignalOutcome.signal_type)
        .order_by(SignalOutcome.signal_type)
    )
    rows = (await session.execute(stmt)).all()
    return [_build_row(r.category, r.total, r.targeted, r.hits) for r in rows]


async def _by_asset(session: AsyncSession) -> list[BreakdownRow]:
    stmt = (
        select(
            SignalOutcome.asset.label("category"),
            func.count().label("total"),
            _TARGETED.label("targeted"),
            _HITS.label("hits"),
        )
        .where(SignalOutcome.evaluated_at.is_not(None))
        .group_by(SignalOutcome.asset)
        .order_by(SignalOutcome.asset)
    )
    rows = (await session.execute(stmt)).all()
    return [_build_row(r.category, r.total, r.targeted, r.hits) for r in rows]


async def _by_direction(session: AsyncSession) -> list[BreakdownRow]:
    stmt = (
        select(
            SignalOutcome.direction.label("category"),
            func.count().label("total"),
            _TARGETED.label("targeted"),
            _HITS.label("hits"),
        )
        .where(SignalOutcome.evaluated_at.is_not(None))
        .group_by(SignalOutcome.direction)
        .order_by(SignalOutcome.direction)
    )
    rows = (await session.execute(stmt)).all()
    return [_build_row(r.category, r.total, r.targeted, r.hits) for r in rows]


async def _by_confidence_bucket(session: AsyncSession) -> list[BreakdownRow]:
    """GROUP BY a CASE expression so the SQL does the bucketing — keeps it
    one roundtrip and lets Postgres scan `confidence` once. Buckets that
    have zero outcomes are filled in with empty rows below so the chart
    always shows the full 4-bucket structure (no "missing bar" UX)."""
    bucket_expr = case(
        (SignalOutcome.confidence <= 3, _CONFIDENCE_BUCKET_LOW),
        (SignalOutcome.confidence <= 6, _CONFIDENCE_BUCKET_MID),
        (SignalOutcome.confidence <= 8, _CONFIDENCE_BUCKET_HIGH),
        else_=_CONFIDENCE_BUCKET_VERY_HIGH,
    ).label("category")

    stmt = (
        select(
            bucket_expr,
            func.count().label("total"),
            _TARGETED.label("targeted"),
            _HITS.label("hits"),
        )
        .where(SignalOutcome.evaluated_at.is_not(None))
        .group_by(bucket_expr)
    )
    rows = (await session.execute(stmt)).all()
    by_label = {r.category: _build_row(r.category, r.total, r.targeted, r.hits) for r in rows}

    # Backfill empty buckets so the FE always renders all 4 bands (the
    # diagnostic value of "the 9–10 bucket is empty" depends on the bucket
    # being visible — collapsing it would hide the finding).
    return [by_label.get(label, _build_row(label, 0, 0, 0)) for label in _CONFIDENCE_BUCKET_ORDER]


async def _histograms(session: AsyncSession) -> tuple[list[HistogramBucket], list[HistogramBucket]]:
    """Fetch MFE + MAE values in ONE query, bucket in Python.

    Same `evaluated_at IS NOT NULL` filter as the categorical breakdowns.
    NULL max_favorable / max_adverse (price fetch failed) are skipped per
    column — we don't drop the entire row if only one of the two is NULL.

    `cast(...AS Numeric)` is defensive — asyncpg returns Decimal for
    NUMERIC columns already, but the cast pins the type so a future
    column-type drift won't silently break the float() conversion.
    """
    stmt = select(
        cast(SignalOutcome.max_favorable, Numeric).label("mfe"),
        cast(SignalOutcome.max_adverse, Numeric).label("mae"),
    ).where(SignalOutcome.evaluated_at.is_not(None))
    rows = (await session.execute(stmt)).all()

    mfe_values: list[Decimal] = [r.mfe for r in rows if r.mfe is not None]
    mae_values: list[Decimal] = [r.mae for r in rows if r.mae is not None]
    return _build_histogram(mfe_values), _build_histogram(mae_values)


# ---------------------------------------------------------------------------
# Cache — 5-min single-key TTLCache. Mirrors the pattern in
# `pipeline/delivery.py:_track_record_cache` (Stage 8-P8): module-level
# TTLCache, single key, miss-falls-through-to-DB. Per CLAUDE.md ("No Redis,
# no Celery") this is in-process — multi-worker deploys would each carry
# their own copy, which is acceptable for a public dashboard (the worst
# case is one extra DB roundtrip per worker every 5 minutes).
#
# Five minutes balances two concerns:
#   * Freshness — outcome eval ticks once an hour; numbers don't actually
#     change minute-to-minute, so a slightly stale cache is invisible.
#   * Stampede protection — a Hacker News link drop won't hit the DB on
#     every page view; just one DB query per 5-min window per worker.
#
# No asyncio.Lock around the populate path — concurrent cache misses
# during a TTL boundary can race and both run the query (5 queries × 2
# concurrent = 10 queries, once every 5 min). Acceptable for this volume;
# add a lock if monitoring ever shows it matters.
# ---------------------------------------------------------------------------


_BREAKDOWN_CACHE_TTL_SEC = 300
_BREAKDOWN_CACHE_KEY = "current"
_breakdown_cache: TTLCache[str, TrackRecordBreakdown] = TTLCache(
    maxsize=1, ttl=_BREAKDOWN_CACHE_TTL_SEC
)


async def get_cached_track_record_breakdown(session: AsyncSession) -> TrackRecordBreakdown:
    """Public, cached entry point — what the HTTP route calls.

    Cache miss: runs `get_track_record_breakdown` (5 queries) and stores.
    Cache hit: zero DB roundtrips, returns the previously-computed instance.

    The uncached `get_track_record_breakdown` stays public so tests can
    bypass the cache without needing to call `_breakdown_cache.clear()`
    on every test (autouse fixture can still clear if a test asserts on
    cache behaviour specifically).
    """
    cached = _breakdown_cache.get(_BREAKDOWN_CACHE_KEY)
    if cached is not None:
        return cached
    fresh = await get_track_record_breakdown(session)
    _breakdown_cache[_BREAKDOWN_CACHE_KEY] = fresh
    return fresh


# ---------------------------------------------------------------------------
# Uncached public surface — preferred for tests and any caller that needs
# a guaranteed-fresh snapshot. Production HTTP path goes through the
# cached wrapper above.
# ---------------------------------------------------------------------------


async def get_track_record_breakdown(session: AsyncSession) -> TrackRecordBreakdown:
    """Build the full diagnostic breakdown in 5 queries.

    Caller owns the transaction (D14). Read-only — no mutations.
    `total_outcomes` is derived from the detector breakdown sum rather
    than a separate COUNT roundtrip; every outcome has a `signal_type`
    so the sum is exact.
    """
    by_detector = await _by_detector(session)
    by_asset = await _by_asset(session)
    by_confidence_bucket = await _by_confidence_bucket(session)
    by_direction = await _by_direction(session)
    mfe_histogram, mae_histogram = await _histograms(session)

    total_outcomes = sum(r.total for r in by_detector)

    log.info(
        "analytics_breakdown_built",
        total_outcomes=total_outcomes,
        detector_categories=len(by_detector),
        asset_categories=len(by_asset),
        direction_categories=len(by_direction),
    )

    return TrackRecordBreakdown(
        total_outcomes=total_outcomes,
        by_detector=by_detector,
        by_asset=by_asset,
        by_confidence_bucket=by_confidence_bucket,
        by_direction=by_direction,
        mfe_histogram=mfe_histogram,
        mae_histogram=mae_histogram,
    )
