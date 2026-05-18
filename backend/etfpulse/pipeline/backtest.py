"""Backtest harness — replay detectors over a historical date range with
candidate threshold configs and score would-be outcomes against historical
klines.

PR I.5. Operator-invoked via `scripts/backtest.py`. Production code paths do
NOT import this module — it's pure backtest plumbing.

What this module does NOT do (and why):
  * **Never writes to production tables.** Outcomes are in-memory dataclasses
    returned to the caller. Persisting would risk collisions on the
    `(fingerprint, signal_date)` unique index when the same hit also fired
    in production.
  * **Never re-runs AI for hits that already have an answer.** The resolver
    chain tries (cache → existing prod Signal → optional live call) in order.
    A typical sweep over the same window pays AI cost AT MOST ONCE per unique
    hit fingerprint.
  * **Does not simulate delivery / confirmation gating.** Backtest measures
    hit-rate — the upstream filter chain (NULL-confidence drop, confirmation
    score, pref-asset match) is a separate analysis.

Reuse contract:
  * Detectors come from `ALL_DETECTORS` shape — we instantiate fresh ones
    per backtest run with override kwargs so a sweep over thresholds
    doesn't mutate the production registry.
  * Look-ahead defense is the detector's responsibility (rule D25). We call
    `detector.detect(session, as_of=T)`; if a detector ever sneaks in a read
    that doesn't honor `as_of`, the regression test in
    `tests/test_pipeline/test_detector_lookahead.py` catches it.
  * Outcome scoring reuses `_compute_metrics` (single-asset) +
    `weighted_composite_return`/`classify_composite_outcome` (MARKET) — same
    rubric as production. No parallel "almost-evaluator".

Transaction contract (D14):
  * `run_backtest` performs reads only — detector queries, kline fetches,
    optional `Signal` lookups for the resolver. No commits, no writes.
  * The CLI wrapper owns the session and rolls back on exit.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Literal, cast

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from etfpulse.constants import MARKET_ASSET
from etfpulse.models import Signal, SignalDirection
from etfpulse.pipeline import ai_cache
from etfpulse.pipeline.analysis import AI_PROMPT_VERSION, AISignalAnalysis
from etfpulse.pipeline.composite_scoring import (
    classify_composite_outcome,
    weighted_composite_return,
)
from etfpulse.pipeline.detectors import DetectorHit
from etfpulse.pipeline.detectors.acceleration import AccelerationDetector
from etfpulse.pipeline.detectors.divergence import DivergenceDetector
from etfpulse.pipeline.detectors.flow_anomaly import FlowAnomalyDetector
from etfpulse.pipeline.detectors.magnitude import MagnitudeDetector
from etfpulse.pipeline.detectors.regime_shift import RegimeShiftDetector
from etfpulse.pipeline.factors import direction_sign_from_action
from etfpulse.pipeline.prices import (
    Asset,
    PriceSource,
    get_daily_klines_from_source,
)
from etfpulse.pipeline.track_record import (
    _composite_endpoints,
    _compute_metrics,
    _pick_close_at,
)

log = structlog.get_logger()


# Default scoring source for backtest klines. MARKET signals (regime_shift)
# always use SoSoValue in production via `_evaluate_market_one`; single-asset
# paths default to the same when `Signal.price_source` is NULL. We pin the
# backtest to "sosovalue" so kline fetches are deterministic across runs.
_BACKTEST_SOURCE: PriceSource = "sosovalue"

# 4h pad mirrors `_KLINE_FETCH_PAD_HOURS` in track_record.py — daily bars whose
# open straddles the t0/end boundary still get included so `_pick_close_at`
# can do the precise selection.
_KLINE_FETCH_PAD_MS = 4 * 3600 * 1000

# Min scorable horizon (mirrors track_record._MIN_SCORABLE_WINDOW_HOURS).
# Scalp (6h) cannot be scored against daily klines (#62). Backtest applies
# the same gate so reported hit rates align with production accounting.
_MIN_SCORABLE_WINDOW_HOURS = 24

# AI-suggested horizon → validity-window hours. Mirrors `_HORIZON_TO_DURATION`
# in pipeline/analysis.py — duplicated as a constant rather than imported
# because `analysis` keeps it as `timedelta` and we want the int-hour form
# both `_evaluate_one` and `_compute_metrics` consume.
_HORIZON_HOURS: dict[str, int] = {"scalp": 6, "swing": 72, "position": 168}


# AI resolver protocol — the orchestrator doesn't care HOW the resolver gets
# its AISignalAnalysis (cache, DB, live), only that it returns one or None.
AIResolver = Callable[[DetectorHit], Awaitable[AISignalAnalysis | None]]


# ---------------------------------------------------------------------------
# Report shapes
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class BacktestOutcomeRow:
    """In-memory analogue of one SignalOutcome row. Never persisted."""

    detector_name: str
    signal_type: str
    asset: str
    signal_date: str  # ISO date — JSON-friendly
    fingerprint: str
    direction: str | None  # "long" / "short" / None when AI declined/missing
    confidence: int | None
    hit_target: bool | None
    hit_stop: bool | None
    composite_return_pct: str | None  # str(Decimal) for lossless JSON
    scoring_version: Literal["v2", "market-v1"] | None
    window_hours: int | None
    skip_reason: str | None  # None when scored; e.g. "no_klines" / "no_direction"


@dataclass(slots=True)
class BacktestPerDetector:
    detector_name: str
    n_hits: int  # all hits regardless of scoring
    n_scored: int  # both hit_target / composite resolved
    wins: int  # hit_target=True OR composite hit
    losses: int  # hit_target=False OR composite miss
    hit_rate: float | None  # wins / n_scored, None when n_scored == 0


@dataclass(slots=True)
class BacktestReport:
    start: str
    end: str
    ai_prompt_version: str
    detector_configs: dict[str, dict[str, Any]]  # name → kwargs applied
    counters: dict[str, int]
    per_detector: list[BacktestPerDetector]
    outcomes: list[BacktestOutcomeRow]

    def to_json_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Detector instantiation
# ---------------------------------------------------------------------------


def _build_detector(name: str, overrides: dict[str, Any]) -> object:
    """Construct a detector with kwargs override. Decimal-typed args coerced."""
    decimal_args = {"min_slope_old_usd", "min_flow_sum_usd"}
    coerced: dict[str, Any] = {}
    for k, v in overrides.items():
        if k in decimal_args and not isinstance(v, Decimal):
            coerced[k] = Decimal(str(v))
        else:
            coerced[k] = v
    ctor = {
        "flow_anomaly": FlowAnomalyDetector,
        "magnitude": MagnitudeDetector,
        "acceleration": AccelerationDetector,
        "divergence": DivergenceDetector,
        "regime_shift": RegimeShiftDetector,
    }[name]
    return ctor(**coerced)


def _default_detectors() -> list[object]:
    """Instantiate each detector with its prod defaults. Backtest sweeps
    override via `detector_overrides`. We don't reuse `ALL_DETECTORS`
    instances because tests / sweeps would share mutable state if we did."""
    from etfpulse.config import settings

    return [
        FlowAnomalyDetector(
            lookback_days=settings.flow_anomaly_lookback_days,
            min_streak_length=settings.flow_anomaly_min_streak_length,
        ),
        MagnitudeDetector(
            lookback_days=settings.magnitude_lookback_days,
            percentile_threshold=settings.magnitude_percentile_threshold,
            min_history_days=settings.magnitude_min_history_days,
        ),
        AccelerationDetector(
            window=settings.acceleration_window,
            change_threshold=settings.acceleration_change_threshold,
            min_slope_old_usd=settings.acceleration_min_slope_old_usd,
        ),
        DivergenceDetector(
            lookback_days=settings.divergence_lookback_days,
            min_price_change_pct=settings.divergence_min_price_change_pct,
            min_flow_sum_usd=settings.divergence_min_flow_sum_usd,
        ),
        RegimeShiftDetector(),
    ]


def _detector_kwargs(d: object) -> dict[str, Any]:
    """Extract the parameters a detector was constructed with for the
    report's `detector_configs` field. Each detector stores its tuned
    knobs as instance attributes; we read them by name."""
    keys = {
        "flow_anomaly": ("lookback_days", "min_streak_length"),
        "magnitude": ("lookback_days", "percentile_threshold", "min_history_days"),
        "acceleration": ("window", "change_threshold", "min_slope_old_usd"),
        "divergence": ("lookback_days", "min_price_change_pct", "min_flow_sum_usd"),
        "regime_shift": (),
    }
    name = getattr(d, "name", "")
    out: dict[str, Any] = {}
    for k in keys.get(name, ()):
        v = getattr(d, k, None)
        # Decimal → str for JSON round-trip without losing precision.
        out[k] = str(v) if isinstance(v, Decimal) else v
    return out


# ---------------------------------------------------------------------------
# AI resolver chain
# ---------------------------------------------------------------------------


def _try_cache_put(fingerprint: str, analysis: AISignalAnalysis) -> None:
    """Cache write that is non-fatal on disk error. Cache is an optimization,
    not correctness — a read-only / full filesystem must not crash a backtest
    mid-sweep after a hit was successfully resolved from the DB."""
    try:
        ai_cache.put(fingerprint=fingerprint, analysis=analysis)
    except OSError as e:
        log.warning(
            "backtest_cache_write_failed",
            fingerprint=fingerprint,
            error=str(e),
        )


def make_resolver(
    session: AsyncSession,
    *,
    allow_live_ai: bool = False,
    live_ai_caller: Callable[[DetectorHit], Awaitable[AISignalAnalysis | None]] | None = None,
) -> AIResolver:
    """Default 3-tier resolver: cache → existing prod Signal → optional live AI.

    * **Cache hit** is the cheap fast path — keyed by `(fingerprint,
      AI_PROMPT_VERSION)`, so a sweep over the same window after the first
      run pays zero AI cost.
    * **Existing prod Signal lookup** is a fallback when cache is cold but
      this fingerprint already fired in production. We read
      `Signal.ai_analysis` and re-validate through `AISignalAnalysis.model_validate`
      so a stored-JSON shape mismatch fails fast rather than producing a
      half-typed analysis. On hit we ALSO write into cache so the next
      sweep skips the DB query.
    * **Live AI** is opt-in via `allow_live_ai`. Disabled by default so a
      misconfigured run doesn't accidentally burn the daily OpenRouter cap.
      Set `live_ai_caller` to provide the actual OpenRouter shim; backtest
      itself never imports the OpenRouter adapter.

    Returns `None` on a full chain miss — the orchestrator marks that hit
    `skip_reason="no_direction"`. No exceptions thrown; cache and DB failures
    log and degrade to the next tier.
    """

    async def _resolve(hit: DetectorHit) -> AISignalAnalysis | None:
        # Tier 1: cache.
        cached = ai_cache.get(fingerprint=hit.fingerprint)
        if cached is not None:
            return cached

        # Tier 2: existing prod Signal with the same fingerprint + prompt version.
        # We filter on `ai_prompt_version` so cohorts don't bleed: a `v2`-built
        # Signal must NOT supply analysis to a `v3` cohort backtest.
        stmt = (
            select(Signal)
            .where(Signal.fingerprint == hit.fingerprint)
            .where(Signal.ai_prompt_version == AI_PROMPT_VERSION)
            .where(Signal.ai_analysis.is_not(None))
            .limit(1)
        )
        row = (await session.execute(stmt)).scalars().first()
        if row is not None and row.ai_analysis:
            try:
                analysis = AISignalAnalysis.model_validate(row.ai_analysis)
            except ValueError as e:
                log.warning(
                    "backtest_existing_signal_shape_invalid",
                    fingerprint=hit.fingerprint,
                    error=str(e),
                )
            else:
                _try_cache_put(hit.fingerprint, analysis)
                return analysis

        # Tier 3: live AI (opt-in).
        if allow_live_ai and live_ai_caller is not None:
            live_result = await live_ai_caller(hit)
            if live_result is not None:
                _try_cache_put(hit.fingerprint, live_result)
                return live_result

        return None

    return _resolve


# ---------------------------------------------------------------------------
# Core orchestrator
# ---------------------------------------------------------------------------


async def run_backtest(
    session: AsyncSession,
    *,
    start: date,
    end: date,
    detector_overrides: dict[str, dict[str, Any]] | None = None,
    ai_resolver: AIResolver | None = None,
) -> BacktestReport:
    """Walk `[start, end]` inclusive, replaying detectors at each T with
    `as_of=T`. Returns an in-memory report; the session is read-only.

    `detector_overrides`: per-detector kwarg overrides keyed by detector name.
    Unspecified detectors run with prod defaults. Unknown names raise (typo
    defense — silently dropping an override would mask a misconfigured sweep).

    `ai_resolver`: defaults to `make_resolver(session, allow_live_ai=False)`
    — i.e. cache + prod-Signal fallback, NO live AI. Pass an explicit
    resolver with `allow_live_ai=True` to allow OpenRouter calls.
    """
    if end < start:
        raise ValueError(f"backtest end {end} < start {start}")

    detector_overrides = detector_overrides or {}
    known = {"flow_anomaly", "magnitude", "acceleration", "divergence", "regime_shift"}
    unknown = set(detector_overrides) - known
    if unknown:
        raise ValueError(f"unknown detector override(s): {sorted(unknown)}")

    # Build the detector list in one pass — replacing each instance with an
    # override-constructed one when matched. Building a new list (rather than
    # mutating the original mid-iteration) is structurally cleaner: there's
    # no implicit dependence on detector classes being uniquely identifiable
    # by `.name`, and a future refactor can't accidentally break iteration
    # by introducing two detectors of the same class.
    detectors: list[object] = []
    for d in _default_detectors():
        name = getattr(d, "name", "")
        if name in detector_overrides:
            detectors.append(_build_detector(name, detector_overrides[name]))
        else:
            detectors.append(d)

    if ai_resolver is None:
        ai_resolver = make_resolver(session)

    detector_configs = {getattr(d, "name", "?"): _detector_kwargs(d) for d in detectors}

    counters: dict[str, int] = {
        "dates_walked": 0,
        "hits_total": 0,
        "hits_unique": 0,
        "hits_duplicate": 0,
        "scored": 0,
        "skipped_no_direction": 0,
        "skipped_invalid_horizon": 0,
        "skipped_intraday_unsupported": 0,
        "skipped_no_klines": 0,
        "skipped_no_bars_in_window": 0,
        "detector_errors": 0,
    }

    outcomes: list[BacktestOutcomeRow] = []
    # Dedupe across the date sweep — a flow_anomaly hit on day 4 may persist
    # across days 5/6/7 in the lookback window until newer data displaces it.
    # We score each unique (fingerprint, signal_date) pair once.
    seen_keys: set[tuple[str, str]] = set()

    cur = start
    while cur <= end:
        counters["dates_walked"] += 1
        for det in detectors:
            try:
                hits = await det.detect(session, as_of=cur)  # type: ignore[attr-defined]
            except Exception as e:  # noqa: BLE001 — D13: one detector cannot kill the cycle.
                counters["detector_errors"] += 1
                log.warning(
                    "backtest_detector_error",
                    detector=getattr(det, "name", "?"),
                    as_of=cur.isoformat(),
                    error=str(e),
                )
                continue
            for hit in hits:
                counters["hits_total"] += 1
                key = (hit.fingerprint, hit.signal_date.isoformat())
                if key in seen_keys:
                    counters["hits_duplicate"] += 1
                    continue
                seen_keys.add(key)
                counters["hits_unique"] += 1
                row = await _score_hit(
                    session, det, hit, ai_resolver=ai_resolver, counters=counters
                )
                outcomes.append(row)
        cur = cur + timedelta(days=1)

    per_detector = _aggregate_per_detector(outcomes)

    return BacktestReport(
        start=start.isoformat(),
        end=end.isoformat(),
        ai_prompt_version=AI_PROMPT_VERSION,
        detector_configs=detector_configs,
        counters=counters,
        per_detector=per_detector,
        outcomes=outcomes,
    )


async def _score_hit(
    session: AsyncSession,
    detector: object,
    hit: DetectorHit,
    *,
    ai_resolver: AIResolver,
    counters: dict[str, int],
) -> BacktestOutcomeRow:
    """Resolve direction via the AI chain, then evaluate against historical
    klines. Single-asset hits route through `_compute_metrics`; MARKET hits
    route through the composite rubric. Always returns a row — `skip_reason`
    is non-None when scoring was abandoned for a non-error reason."""
    analysis = await ai_resolver(hit)
    base = _base_row(detector, hit)
    if analysis is None:
        counters["skipped_no_direction"] += 1
        return _with_skip(base, "no_direction")

    direction_sign = direction_sign_from_action(analysis.suggested_action)
    if direction_sign == 0:
        counters["skipped_no_direction"] += 1
        return _with_skip(
            base,
            "no_direction",
            direction="wait",
            confidence=analysis.confidence,
        )

    window_hours = _HORIZON_HOURS.get(analysis.time_horizon)
    if window_hours is None:
        counters["skipped_invalid_horizon"] += 1
        return _with_skip(base, "invalid_horizon", confidence=analysis.confidence)
    if window_hours < _MIN_SCORABLE_WINDOW_HOURS:
        counters["skipped_intraday_unsupported"] += 1
        return _with_skip(
            base,
            "intraday_unsupported",
            confidence=analysis.confidence,
            window_hours=window_hours,
        )

    # `t0` for a backtest hit = the daily cron time on `signal_date + 1`.
    # In production, `Signal.created_at` is stamped by APScheduler when the
    # daily cycle fires (`scheduler_cron_hour`:`scheduler_cron_minute` UTC,
    # defaults 04:30). `_compute_metrics` includes a bar B in its window iff
    # `t0_ms <= B.timestamp_ms <= horizon_end_ms` (both bounds INCLUSIVE).
    # Anchoring t0 at midnight UTC of signal_date+1 (= the bar's exact open
    # timestamp) would include that bar — which in production was BEFORE
    # the signal fired and therefore excluded. Shifting t0 forward by the
    # cron offset reproduces the exact production window (D+2..D+4 for a
    # 72h swing) so `max_favorable` / `hit_target` / etc. agree with what
    # the live evaluator would have written.
    #
    # Edge case: an operator with `SCHEDULER_CRON_HOUR=0` +
    # `SCHEDULER_CRON_MINUTE=0` would otherwise put t0 exactly at midnight
    # — back to the original off-by-one. Floor the offset at 1 minute past
    # midnight; any positive intra-day offset gives the same daily-aligned
    # window, so this floor is a no-op for every non-pathological cron
    # config.
    from etfpulse.config import settings as _settings

    cron_h = _settings.scheduler_cron_hour
    cron_m = _settings.scheduler_cron_minute
    if cron_h == 0 and cron_m == 0:
        cron_m = 1
    t0_dt = datetime.combine(
        hit.signal_date + timedelta(days=1),
        time(hour=cron_h, minute=cron_m),
        tzinfo=UTC,
    )
    t0_ms = int(t0_dt.timestamp() * 1000)
    horizon_ms = window_hours * 3600 * 1000

    if hit.asset == MARKET_ASSET:
        return await _score_market(
            base=base,
            direction_sign=direction_sign,
            analysis=analysis,
            t0_ms=t0_ms,
            horizon_ms=horizon_ms,
            window_hours=window_hours,
            counters=counters,
        )
    return await _score_single_asset(
        base=base,
        direction_sign=direction_sign,
        analysis=analysis,
        asset=hit.asset,
        t0_ms=t0_ms,
        horizon_ms=horizon_ms,
        window_hours=window_hours,
        counters=counters,
    )


def _base_row(detector: object, hit: DetectorHit) -> BacktestOutcomeRow:
    return BacktestOutcomeRow(
        detector_name=getattr(detector, "name", "?"),
        signal_type=hit.signal_type,
        asset=hit.asset,
        signal_date=hit.signal_date.isoformat(),
        fingerprint=hit.fingerprint,
        direction=None,
        confidence=None,
        hit_target=None,
        hit_stop=None,
        composite_return_pct=None,
        scoring_version=None,
        window_hours=None,
        skip_reason=None,
    )


def _with_skip(
    row: BacktestOutcomeRow,
    reason: str,
    *,
    direction: str | None = None,
    confidence: int | None = None,
    window_hours: int | None = None,
) -> BacktestOutcomeRow:
    row.skip_reason = reason
    if direction is not None:
        row.direction = direction
    if confidence is not None:
        row.confidence = confidence
    if window_hours is not None:
        row.window_hours = window_hours
    return row


async def _score_single_asset(
    *,
    base: BacktestOutcomeRow,
    direction_sign: int,
    analysis: AISignalAnalysis,
    asset: str,
    t0_ms: int,
    horizon_ms: int,
    window_hours: int,
    counters: dict[str, int],
) -> BacktestOutcomeRow:
    if asset not in ("BTC", "ETH"):
        return _with_skip(base, "unsupported_asset", confidence=analysis.confidence)
    asset_typed: Asset = asset  # type: ignore[assignment]

    bars = await get_daily_klines_from_source(
        asset_typed,
        _BACKTEST_SOURCE,
        start_time_ms=t0_ms - _KLINE_FETCH_PAD_MS,
        end_time_ms=t0_ms + horizon_ms + _KLINE_FETCH_PAD_MS,
    )
    if bars is None:
        counters["skipped_no_klines"] += 1
        return _with_skip(base, "no_klines", confidence=analysis.confidence)

    # `price_at_signal` baseline mirrors `_evaluate_one` lookup: AI-suggested
    # entry takes precedence; absent that we fall back to the close at t0
    # (the bar containing the as_of/signal-fire boundary). `_pick_close_at`
    # returns None when no bars open ≤ t0 — abandon scoring with a clear
    # reason rather than guessing.
    entry: Decimal | None = analysis.entry_price
    if entry is None or entry <= 0:
        baseline = _pick_close_at(bars, t0_ms)
        if baseline is None:
            counters["skipped_no_bars_in_window"] += 1
            return _with_skip(
                base, "no_bars_in_window", confidence=analysis.confidence, window_hours=window_hours
            )
        entry = baseline

    direction = SignalDirection.LONG if direction_sign == 1 else SignalDirection.SHORT
    metrics = _compute_metrics(
        direction=direction,
        entry=entry,
        stop=analysis.stop_price,
        target=analysis.target_price,
        t0_ms=t0_ms,
        window_hours=window_hours,
        bars=bars,
    )
    if metrics is None:
        counters["skipped_no_bars_in_window"] += 1
        return _with_skip(
            base, "no_bars_in_window", confidence=analysis.confidence, window_hours=window_hours
        )

    counters["scored"] += 1
    base.direction = direction.value
    base.confidence = analysis.confidence
    base.hit_target = metrics.hit_target
    base.hit_stop = metrics.hit_stop
    base.scoring_version = "v2"
    base.window_hours = window_hours
    return base


async def _score_market(
    *,
    base: BacktestOutcomeRow,
    direction_sign: int,
    analysis: AISignalAnalysis,
    t0_ms: int,
    horizon_ms: int,
    window_hours: int,
    counters: dict[str, int],
) -> BacktestOutcomeRow:
    from etfpulse.config import settings

    btc_bars = await get_daily_klines_from_source(
        "BTC",
        _BACKTEST_SOURCE,
        start_time_ms=t0_ms - _KLINE_FETCH_PAD_MS,
        end_time_ms=t0_ms + horizon_ms + _KLINE_FETCH_PAD_MS,
    )
    eth_bars = await get_daily_klines_from_source(
        "ETH",
        _BACKTEST_SOURCE,
        start_time_ms=t0_ms - _KLINE_FETCH_PAD_MS,
        end_time_ms=t0_ms + horizon_ms + _KLINE_FETCH_PAD_MS,
    )
    if btc_bars is None or eth_bars is None:
        counters["skipped_no_klines"] += 1
        return _with_skip(base, "no_klines", confidence=analysis.confidence)

    btc_baseline, btc_end = _composite_endpoints(btc_bars, t0_ms, horizon_ms)
    eth_baseline, eth_end = _composite_endpoints(eth_bars, t0_ms, horizon_ms)
    if (
        btc_baseline is None
        or btc_end is None
        or eth_baseline is None
        or eth_end is None
        or btc_baseline <= 0
        or eth_baseline <= 0
    ):
        counters["skipped_no_bars_in_window"] += 1
        return _with_skip(
            base, "no_bars_in_window", confidence=analysis.confidence, window_hours=window_hours
        )

    btc_return = (btc_end - btc_baseline) / btc_baseline
    eth_return = (eth_end - eth_baseline) / eth_baseline
    composite = weighted_composite_return(
        btc_return_pct=btc_return,
        eth_return_pct=eth_return,
        btc_weight=settings.market_composite_weight_btc,
        eth_weight=settings.market_composite_weight_eth,
    )
    hit = classify_composite_outcome(
        composite_return_pct=composite,
        # `direction_sign` is one of -1/0/+1 from `direction_sign_from_action`;
        # `direction_sign == 0` was rejected upstream in `_score_hit`, so the
        # value here is exactly the Literal[-1, 1] subset (we still cast to
        # the full Literal[-1, 0, 1] mypy expects).
        direction=cast(Literal[-1, 0, 1], direction_sign),
        hit_pct=settings.market_composite_hit_pct,
    )
    assert hit is not None  # noqa: S101 — direction_sign==0 short-circuited upstream

    counters["scored"] += 1
    base.direction = (
        SignalDirection.LONG.value if direction_sign == 1 else SignalDirection.SHORT.value
    )
    base.confidence = analysis.confidence
    base.hit_target = hit
    base.hit_stop = None
    base.composite_return_pct = str(composite)
    base.scoring_version = "market-v1"
    base.window_hours = window_hours
    return base


def _aggregate_per_detector(outcomes: list[BacktestOutcomeRow]) -> list[BacktestPerDetector]:
    """Group rows by detector_name, count hits / scored / wins / losses,
    derive hit_rate. Detector order = `ALL_DETECTORS` order so the report
    is stable across runs."""
    detector_order = ["flow_anomaly", "magnitude", "acceleration", "divergence", "regime_shift"]
    buckets: dict[str, BacktestPerDetector] = {
        name: BacktestPerDetector(
            detector_name=name, n_hits=0, n_scored=0, wins=0, losses=0, hit_rate=None
        )
        for name in detector_order
    }
    for row in outcomes:
        b = buckets.get(row.detector_name)
        if b is None:
            # Unknown detector name — shouldn't happen, but don't drop on the floor.
            buckets[row.detector_name] = BacktestPerDetector(
                detector_name=row.detector_name,
                n_hits=0,
                n_scored=0,
                wins=0,
                losses=0,
                hit_rate=None,
            )
            b = buckets[row.detector_name]
        b.n_hits += 1
        if row.skip_reason is None and row.hit_target is not None:
            b.n_scored += 1
            if row.hit_target:
                b.wins += 1
            else:
                b.losses += 1
    for b in buckets.values():
        if b.n_scored > 0:
            b.hit_rate = round(b.wins / b.n_scored, 4)
    return list(buckets.values())


__all__ = [
    "AIResolver",
    "BacktestOutcomeRow",
    "BacktestPerDetector",
    "BacktestReport",
    "make_resolver",
    "run_backtest",
]
