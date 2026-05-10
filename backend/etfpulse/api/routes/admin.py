"""Admin-only routes for manual pipeline operations + operator metrics.

All routes in this module are gated by `require_admin_key` — missing or
empty `ADMIN_API_KEY` env returns 503 (admin surface disabled), wrong key
returns 401.

Anti-drift: routes call `_run_cycle_with_session` from the scheduler module
so the admin trigger is the SAME code path as the scheduled cron (D15). If
a bug breaks the cycle for one surface, it breaks for both — they can't
silently drift.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from etfpulse.api.deps import get_db_session, require_admin_key
from etfpulse.api.schemas.admin import (
    AdminMetrics,
    DeliveryStatusCounts,
    SchedulerJobInfo,
    SignalStatusCounts,
)
from etfpulse.config import settings
from etfpulse.models import DeliveryStatus, Signal, SignalDelivery, SignalStatus
from etfpulse.pipeline.reapers import DELIVERY_REAPER_ERROR
from etfpulse.pipeline.scheduler import _run_cycle_with_session

log = structlog.get_logger()
router = APIRouter(prefix="/admin", tags=["admin"])


@router.post(
    "/signals/trigger",
    dependencies=[Depends(require_admin_key)],
    # Not part of the product OpenAPI — operator-only.
    include_in_schema=False,
)
async def trigger_signal_cycle() -> dict[str, Any]:
    """Fire one run of the daily signal cycle, synchronously.

    Returns the same summary dict that the scheduled cron logs. Useful for
    backfills, post-deploy smoke checks, and "why did no signals fire
    today?" debugging.

    Synchronous by design — the caller waits for the full cycle (up to
    ~60s in prod). If that becomes painful, switch to a background task
    and return 202 Accepted.
    """
    log.info("admin_trigger_cycle_begin")
    summary = await _run_cycle_with_session()
    if summary is None:
        # The cycle rolled back; details are in server logs. Don't leak
        # internals in the response body.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="cycle failed — see server logs",
        )
    log.info("admin_trigger_cycle_done", **summary)
    return summary


@router.get(
    "/metrics",
    response_model=AdminMetrics,
    dependencies=[Depends(require_admin_key)],
    include_in_schema=False,
)
async def get_admin_metrics(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> AdminMetrics:
    """Point-in-time operator view of signal + delivery queue health.

    Reads only — no mutations. Surfaces the four conditions a reaper would
    act on (overdue signals, stuck deliveries) so an operator can tell
    whether the reapers are keeping up before the next scheduled tick.

    Implemented as four GROUP-BY queries + two scalar counts. No JOINs,
    no row materialisation — should stay fast as the tables grow.
    """
    now = datetime.now(UTC)
    stuck_cutoff = now - timedelta(seconds=settings.delivery_pending_max_age_seconds)

    # --- Signal status counts (GROUP BY) -----------------------------------
    sig_rows = (
        await session.execute(select(Signal.status, func.count()).group_by(Signal.status))
    ).all()
    sig_by_status = {row[0]: row[1] for row in sig_rows}
    signal_status = SignalStatusCounts(
        pending=sig_by_status.get(SignalStatus.PENDING.value, 0),
        alerted=sig_by_status.get(SignalStatus.ALERTED.value, 0),
        expired=sig_by_status.get(SignalStatus.EXPIRED.value, 0),
    )

    # --- Delivery status counts (GROUP BY) ---------------------------------
    deliv_rows = (
        await session.execute(
            select(SignalDelivery.status, func.count()).group_by(SignalDelivery.status)
        )
    ).all()
    deliv_by_status = {row[0]: row[1] for row in deliv_rows}
    delivery_status = DeliveryStatusCounts(
        pending=deliv_by_status.get(DeliveryStatus.PENDING.value, 0),
        delivered=deliv_by_status.get(DeliveryStatus.DELIVERED.value, 0),
        failed=deliv_by_status.get(DeliveryStatus.FAILED.value, 0),
        skipped=deliv_by_status.get(DeliveryStatus.SKIPPED.value, 0),
    )

    # --- Overdue-but-unreaped signals --------------------------------------
    # Same WHERE as `expire_overdue_signals`. Should be ≈0 between reaper
    # ticks; persistent non-zero = scheduler problem.
    overdue_stmt = select(func.count()).where(
        Signal.status != SignalStatus.EXPIRED.value,
        Signal.expires_at.is_not(None),
        Signal.expires_at < now,
    )
    signals_overdue_unreaped = int((await session.execute(overdue_stmt)).scalar_one())

    # --- AI-failed accumulation -------------------------------------------
    null_conf_stmt = select(func.count()).where(Signal.confidence.is_(None))
    signals_null_confidence = int((await session.execute(null_conf_stmt)).scalar_one())

    # --- Stuck PENDING deliveries -----------------------------------------
    # Same WHERE as `fail_stuck_deliveries` — count of rows the reaper
    # would touch on its next tick.
    stuck_stmt = select(func.count()).where(
        SignalDelivery.status == DeliveryStatus.PENDING.value,
        SignalDelivery.created_at < stuck_cutoff,
    )
    deliveries_stuck_pending = int((await session.execute(stuck_stmt)).scalar_one())

    # --- All-time reaper-failure count ------------------------------------
    reaper_fail_stmt = select(func.count()).where(
        SignalDelivery.error_message == DELIVERY_REAPER_ERROR,
    )
    deliveries_reaper_failures = int((await session.execute(reaper_fail_stmt)).scalar_one())

    # --- Scheduler job introspection --------------------------------------
    # `app.state.scheduler` is set by `start_scheduler` when `run_scheduler`
    # is on; absent when scheduler is disabled. None signals "scheduler off"
    # so the operator UI can render "scheduler disabled" rather than
    # "no jobs registered" (different failure modes).
    scheduler = getattr(request.app.state, "scheduler", None)
    scheduler_jobs: list[SchedulerJobInfo] | None
    if scheduler is None:
        scheduler_jobs = None
    else:
        scheduler_jobs = [
            SchedulerJobInfo(
                id=job.id,
                next_run_at=job.next_run_time,
                trigger=str(job.trigger),
                pending=job.pending,
            )
            for job in scheduler.get_jobs()
        ]

    return AdminMetrics(
        signal_status_counts=signal_status,
        delivery_status_counts=delivery_status,
        signals_overdue_unreaped=signals_overdue_unreaped,
        signals_null_confidence=signals_null_confidence,
        deliveries_stuck_pending=deliveries_stuck_pending,
        deliveries_reaper_failures=deliveries_reaper_failures,
        scheduler_jobs=scheduler_jobs,
    )
