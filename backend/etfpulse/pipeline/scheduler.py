"""APScheduler wiring for the daily signal cycle.

Exports `start_scheduler(app)` — a `StartupTask` (per `api/lifespan.py`'s
contract) that the lifespan composes alongside the bot and any future
background workers.

Key behaviours:
    - Pinned to UTC (`timezone="UTC"` on both the scheduler and CronTrigger).
      Resolution to issue #31 — the cron is documented as UTC in
      `.env.example` and config.py; this is where that documentation
      becomes load-bearing.
    - `max_instances=1, coalesce=True` (Resolution R8) — if a cycle ever runs
      longer than 24h, the next fire is suppressed rather than queued.
    - Catch-up at startup (Resolution R9 / Decision R-catchup): on boot, if
      `MAX(etf_flows.captured_at)` is NULL or more than 1 day stale, schedule
      a one-shot DateTrigger job that runs the cycle ~1s later. Means a
      container restart that missed yesterday's cron self-heals without
      manual intervention.
    - Disabled when `settings.run_scheduler=False` (Resolution R12) — the
      contextmanager yields immediately with no jobs registered. Useful for
      separating scheduler into a worker process later.

Anti-drift rule installed by this stage:
    D15 — All scheduled jobs go through this scheduler. Never spawn raw
          asyncio background tasks for periodic work — they bypass
          `max_instances`/`coalesce` and become invisible to introspection.
          One-shot jobs (e.g. catch-up) use APScheduler's DateTrigger so
          they're still listed in `get_jobs()`.

Known limitations:
    - `shutdown(wait=False)` returns instantly. In-flight jobs become orphan
      asyncio tasks that FastAPI cancels on event-loop teardown. Issue #28
      tracks the deeper graceful-shutdown work — for Wave 1 the simpler
      behaviour is fine.
    - Counter / state for OpenRouter cap is process-local (issue #12).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from etfpulse.config import settings
from etfpulse.db import async_session
from etfpulse.models import ETFFlow
from etfpulse.pipeline.signal_builder import run_daily_cycle

log = structlog.get_logger()

_DAILY_JOB_ID = "daily_cycle"
_CATCHUP_JOB_ID = "catchup"
_CATCHUP_DELAY_SECONDS = 1  # tiny delay so the scheduler picks it up cleanly


async def _needs_catchup(session: AsyncSession) -> bool:
    """True if no ETFFlow data exists OR the latest row is >1 day old.

    The threshold is strictly `> 1 day` so steady-state operation (latest
    captured_at = yesterday, because SoSoValue publishes EOD overnight)
    does NOT trigger catch-up on every boot — only genuinely missed cycles do.
    """
    result = await session.execute(select(func.max(ETFFlow.captured_at)))
    latest = result.scalar()
    if latest is None:
        log.info("scheduler_catchup_check", reason="empty_table", needed=True)
        return True
    today = datetime.now(UTC).date()
    age = today - latest
    needed = age > timedelta(days=1)
    log.info(
        "scheduler_catchup_check",
        latest=str(latest),
        age_days=age.days,
        needed=needed,
    )
    return needed


async def _run_cycle_with_session() -> None:
    """Production cycle wrapper — opens a session, commits on success.

    Called by APScheduler (cron job and catch-up job). Owns the transaction
    boundary; `run_daily_cycle` itself does not commit (D14).
    """
    async with async_session() as session:
        try:
            summary = await run_daily_cycle(session)
            await session.commit()
            log.info("scheduled_cycle_committed", **summary)
        except Exception as exc:
            await session.rollback()
            log.error(
                "scheduled_cycle_failed",
                error_type=type(exc).__name__,
                error=str(exc),
                exc_info=exc,
            )


@asynccontextmanager
async def start_scheduler(app: FastAPI) -> AsyncIterator[None]:
    """StartupTask — wire APScheduler around `run_daily_cycle`.

    Yields after startup is complete. On exit, shuts down the scheduler
    immediately (in-flight jobs become orphan asyncio tasks).
    """
    if not settings.run_scheduler:
        log.info("scheduler_disabled", reason="run_scheduler=false")
        yield
        return

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        _run_cycle_with_session,
        trigger=CronTrigger(
            hour=settings.scheduler_cron_hour,
            minute=settings.scheduler_cron_minute,
            timezone="UTC",
        ),
        id=_DAILY_JOB_ID,
        max_instances=1,
        coalesce=True,
    )

    # Catch-up check — opens its own short-lived session so the scheduler
    # contextmanager doesn't depend on FastAPI request scope.
    async with async_session() as session:
        catchup_needed = await _needs_catchup(session)

    if catchup_needed:
        scheduler.add_job(
            _run_cycle_with_session,
            trigger=DateTrigger(
                run_date=datetime.now(UTC) + timedelta(seconds=_CATCHUP_DELAY_SECONDS)
            ),
            id=_CATCHUP_JOB_ID,
            max_instances=1,
        )
        log.info("scheduler_catchup_scheduled", delay_seconds=_CATCHUP_DELAY_SECONDS)

    scheduler.start()
    app.state.scheduler = scheduler
    log.info(
        "scheduler_started",
        cron_hour=settings.scheduler_cron_hour,
        cron_minute=settings.scheduler_cron_minute,
        jobs=[j.id for j in scheduler.get_jobs()],
    )

    try:
        yield
    finally:
        log.info("scheduler_shutdown_begin")
        # `wait=False` — see module docstring + issue #28.
        scheduler.shutdown(wait=False)
        log.info("scheduler_shutdown_complete")
