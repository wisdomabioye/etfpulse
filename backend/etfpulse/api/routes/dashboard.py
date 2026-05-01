"""Public dashboard stats — aggregate counters for the home page tiles.

Single-query aggregation using Postgres's `FILTER` clause so we don't run
four separate scans of `signals`. `hit_rate_72h` is deliberately omitted
until Stage 08 lands (depends on `SignalOutcome` rows — see open_issues #44).

No auth (Wave 1 scope per open_issues #43).
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
from etfpulse.models import Signal
from etfpulse.pipeline.regime_monitor import get_latest_regime

log = structlog.get_logger()
router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/stats", response_model=DashboardStats)
async def get_stats(session: AsyncSession = Depends(get_db_session)) -> DashboardStats:
    """Return the home-page headline tiles + the latest regime indicator.

    Empty-DB safe: `AVG` over no rows returns NULL; `MAX` over no rows
    returns NULL. Pydantic's `float | None` field shapes accept both.
    Regime fields are None when no `regime_snapshots` row exists yet.

    Two roundtrips fired in parallel via `asyncio.gather`: the aggregate
    over `signals` + a single-row lookup on `regime_snapshots` (via
    `get_latest_regime` so this endpoint and `/api/regime` cannot drift
    on what "latest" means). Parallel because they're independent — sequential
    awaits would double the endpoint latency for no gain.
    """
    now = datetime.now(UTC)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    stmt = select(
        func.count().label("total_signals"),
        func.count().filter(Signal.created_at >= today_start).label("signals_today"),
        func.avg(Signal.confidence).label("avg_confidence"),
        func.max(Signal.created_at).label("last_signal_at"),
    ).select_from(Signal)

    aggregate_result, snapshot = await asyncio.gather(
        session.execute(stmt),
        get_latest_regime(session),
    )
    row = aggregate_result.one()

    # Postgres AVG returns Decimal (or NULL). Cast to float for clean JSON
    # and to respect the DashboardStats schema. NULL passes through to None.
    avg = row.avg_confidence
    avg_float = float(avg) if avg is not None else None

    # Legacy pre-Stage-7 rows (NULL regime/posture columns) are surfaced
    # as None rather than the raw string, matching the schema contract.
    current_regime = snapshot.regime if snapshot is not None else None
    signal_posture = snapshot.signal_posture if snapshot is not None else None

    return DashboardStats(
        total_signals=row.total_signals,
        signals_today=row.signals_today,
        avg_confidence=avg_float,
        last_signal_at=row.last_signal_at,
        current_regime=current_regime,
        signal_posture=signal_posture,
    )
