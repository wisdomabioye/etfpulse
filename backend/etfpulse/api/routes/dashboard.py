"""Public dashboard stats — aggregate counters for the home page tiles.

Single-query aggregation using Postgres's `FILTER` clause so we don't run
four separate scans of `signals`. Stage 8-P5 (closes open_issues #44) added
a parallel aggregate over `signal_outcomes` for the `hit_rate_72h` headline.
PR E.1 added two LIMIT-1 lookups (`last_target_hit`, `last_stop_saved`) for
the home page hero card.

No auth (Phase 1 scope per open_issues #43).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import structlog
from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from etfpulse.api.deps import get_db_session
from etfpulse.api.schemas.dashboard import DashboardStats, HeroOutcome
from etfpulse.models import Signal, SignalOutcome
from etfpulse.pipeline.regime_monitor import get_latest_regime
from etfpulse.pipeline.track_record import compute_hit_rate_pct, evaluated_outcomes_predicate

log = structlog.get_logger()
router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/stats", response_model=DashboardStats)
async def get_stats(session: AsyncSession = Depends(get_db_session)) -> DashboardStats:
    """Return the home-page headline tiles + the latest regime indicator.

    Empty-DB safe: `AVG` over no rows returns NULL; `MAX` over no rows
    returns NULL. Pydantic's `float | None` field shapes accept both.
    Regime fields are None when no `regime_snapshots` row exists yet.
    Hit-rate fields are None / 0 when no SignalOutcome rows exist yet
    (cold-boot before signals have aged past the 72h eval delay). The PR E.1
    hero fields (`last_target_hit`, `last_stop_saved`) are None when no
    outcome matches the strict selection rules.

    Five roundtrips fired in parallel via `asyncio.gather`:
        1. Aggregate over `signals` (count/today/avg/max-created-at)
        2. Single-row lookup on `regime_snapshots` (via `get_latest_regime`
           so this endpoint and `/api/regime` cannot drift on "latest")
        3. Aggregate over `signal_outcomes` (eval count + targeted count
           + targets-hit count for hit_rate_72h)
        4. PR E.1 — latest `SignalOutcome` with `hit_target=True` + non-NULL
           levels, joined to its parent `signals` row for the headline.
        5. PR E.1 — latest `SignalOutcome` with `hit_stop=True` + non-NULL
           levels + `max_adverse > 0`, joined the same way.

    All five are independent — gather amortises the latency to the longest
    single query rather than the sum of five.
    """
    now = datetime.now(UTC)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    signals_stmt = select(
        func.count().label("total_signals"),
        func.count().filter(Signal.created_at >= today_start).label("signals_today"),
        func.avg(Signal.confidence).label("avg_confidence"),
        func.max(Signal.created_at).label("last_signal_at"),
    ).select_from(Signal)

    # Outcomes aggregate — `targeted_count` is the denominator for
    # hit_rate_72h (signals where AI set a target — see
    # `api/routes/track_record.py` for the same rationale).
    outcomes_stmt = (
        select(
            func.count().label("evaluated_count"),
            func.count().filter(SignalOutcome.hit_target.is_(True)).label("targets_hit"),
            func.count().filter(SignalOutcome.hit_target.is_not(None)).label("targeted_count"),
        )
        .select_from(SignalOutcome)
        .where(evaluated_outcomes_predicate())
    )

    # PR E.1 — hero card queries. Two separate LIMIT-1 selects (one per
    # outcome bucket) rather than a CTE — clearer SQL, same plan at Phase 1
    # scale, and `asyncio.gather` already amortises the round-trip cost.
    # `evaluated_outcomes_predicate()` is explicit so a NULL never sorts
    # ahead of a real value regardless of Postgres default null-ordering,
    # AND the definition stays in lockstep with calibration / per-detector
    # / track-record per D22.
    target_hit_stmt = (
        select(SignalOutcome, Signal)
        .join(Signal, Signal.id == SignalOutcome.signal_id)
        .where(
            SignalOutcome.hit_target.is_(True),
            SignalOutcome.entry_price.is_not(None),
            SignalOutcome.target_price.is_not(None),
            evaluated_outcomes_predicate(),
        )
        .order_by(SignalOutcome.evaluated_at.desc())
        .limit(1)
    )
    # "Stop saved" requires a real adverse move — `max_adverse > 0` filters
    # rows where the trade hit the stop with no underwater movement at all,
    # which would frame a non-event as a "save."
    stop_saved_stmt = (
        select(SignalOutcome, Signal)
        .join(Signal, Signal.id == SignalOutcome.signal_id)
        .where(
            SignalOutcome.hit_stop.is_(True),
            SignalOutcome.entry_price.is_not(None),
            SignalOutcome.stop_price.is_not(None),
            SignalOutcome.max_adverse.is_not(None),
            SignalOutcome.max_adverse > Decimal("0"),
            evaluated_outcomes_predicate(),
        )
        .order_by(SignalOutcome.evaluated_at.desc())
        .limit(1)
    )

    (
        signals_result,
        snapshot,
        outcomes_result,
        target_hit_result,
        stop_saved_result,
    ) = await asyncio.gather(
        session.execute(signals_stmt),
        get_latest_regime(session),
        session.execute(outcomes_stmt),
        session.execute(target_hit_stmt),
        session.execute(stop_saved_stmt),
    )
    row = signals_result.one()
    outcome_row = outcomes_result.one()
    last_target_hit = _build_hero_outcome(target_hit_result.first())
    last_stop_saved = _build_hero_outcome(stop_saved_result.first())

    # Postgres AVG returns Decimal (or NULL). Cast to float for clean JSON
    # and to respect the DashboardStats schema. NULL passes through to None.
    avg = row.avg_confidence
    avg_float = float(avg) if avg is not None else None

    # Legacy pre-Stage-7 rows (NULL regime/posture columns) are surfaced
    # as None rather than the raw string, matching the schema contract.
    current_regime = snapshot.regime if snapshot is not None else None
    signal_posture = snapshot.signal_posture if snapshot is not None else None

    # Hit rate as PERCENT (0..100) — same unit as
    # `/api/track-record.summary.hit_rate_pct` so the FE never converts
    # between fraction and percent. None when no signal had a target —
    # better than rendering "0%" for an empty cohort.
    targeted = outcome_row.targeted_count
    hit_rate_global = compute_hit_rate_pct(outcome_row.targets_hit, targeted)

    return DashboardStats(
        total_signals=row.total_signals,
        signals_today=row.signals_today,
        avg_confidence=avg_float,
        last_signal_at=row.last_signal_at,
        current_regime=current_regime,
        signal_posture=signal_posture,
        # PR B (#60) — `hit_rate_global` is the v2 name; `hit_rate_72h` is
        # the deprecated parallel kept for one release cycle. Both carry
        # the same value to avoid two-truth drift while the frontend rolls
        # over. Drop `hit_rate_72h` once the v2 frontend is the only
        # consumer in production.
        hit_rate_global=hit_rate_global,
        hit_rate_72h=hit_rate_global,
        evaluated_count=outcome_row.evaluated_count,
        last_target_hit=last_target_hit,
        last_stop_saved=last_stop_saved,
    )


def _build_hero_outcome(row: tuple | None) -> HeroOutcome | None:
    """Map a `(SignalOutcome, Signal)` row to a `HeroOutcome` DTO, or None.

    Pulled into a helper because both hero slots use the identical mapping;
    inlining twice would invite drift on the next field addition. `headline`
    comes from `signal.ai_analysis` (JSONB → dict in Python) without a SQL
    cast — keeps the SQL portable and avoids `.astext` quoting subtleties.
    """
    if row is None:
        return None
    outcome, signal = row
    headline = None
    if isinstance(signal.ai_analysis, dict):
        h = signal.ai_analysis.get("headline")
        if isinstance(h, str):
            headline = h
    return HeroOutcome(
        signal_id=outcome.signal_id,
        asset=outcome.asset,
        signal_type=outcome.signal_type,
        direction=outcome.direction,
        confidence=outcome.confidence,
        headline=headline,
        entry_price=outcome.entry_price,
        stop_price=outcome.stop_price,
        target_price=outcome.target_price,
        price_at_signal=outcome.price_at_signal,
        max_favorable=outcome.max_favorable,
        max_adverse=outcome.max_adverse,
        evaluated_at=outcome.evaluated_at,
        signal_created_at=signal.created_at,
    )
