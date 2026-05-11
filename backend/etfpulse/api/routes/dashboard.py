"""Public dashboard stats — aggregate counters for the home page tiles.

Single-query aggregation using Postgres's `FILTER` clause so we don't run
four separate scans of `signals`. Stage 8-P5 (closes open_issues #44) added
a parallel aggregate over `signal_outcomes` for the `hit_rate_72h` headline.

No auth (Phase 1 scope per open_issues #43).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from etfpulse.api.deps import get_db_session
from etfpulse.api.schemas.dashboard import DashboardStats
from etfpulse.models import Signal, SignalOutcome
from etfpulse.pipeline.regime_monitor import get_latest_regime
from etfpulse.pipeline.track_record import compute_hit_rate_pct

log = structlog.get_logger()
router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/stats", response_model=DashboardStats)
async def get_stats(session: AsyncSession = Depends(get_db_session)) -> DashboardStats:
    """Return the home-page headline tiles + the latest regime indicator.

    Empty-DB safe: `AVG` over no rows returns NULL; `MAX` over no rows
    returns NULL. Pydantic's `float | None` field shapes accept both.
    Regime fields are None when no `regime_snapshots` row exists yet.
    Hit-rate fields are None / 0 when no SignalOutcome rows exist yet
    (cold-boot before signals have aged past the 72h eval delay).

    Three roundtrips fired in parallel via `asyncio.gather`:
        1. Aggregate over `signals` (count/today/avg/max-created-at)
        2. Single-row lookup on `regime_snapshots` (via `get_latest_regime`
           so this endpoint and `/api/regime` cannot drift on "latest")
        3. Aggregate over `signal_outcomes` (eval count + targeted count
           + targets-hit count for hit_rate_72h)

    All three are independent — gather amortises the latency to the
    longest single query rather than the sum of three.
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
        .where(SignalOutcome.evaluated_at.is_not(None))
    )

    signals_result, snapshot, outcomes_result = await asyncio.gather(
        session.execute(signals_stmt),
        get_latest_regime(session),
        session.execute(outcomes_stmt),
    )
    row = signals_result.one()
    outcome_row = outcomes_result.one()

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
    hit_rate_72h = compute_hit_rate_pct(outcome_row.targets_hit, targeted)

    return DashboardStats(
        total_signals=row.total_signals,
        signals_today=row.signals_today,
        avg_confidence=avg_float,
        last_signal_at=row.last_signal_at,
        current_regime=current_regime,
        signal_posture=signal_posture,
        hit_rate_72h=hit_rate_72h,
        evaluated_count=outcome_row.evaluated_count,
    )
