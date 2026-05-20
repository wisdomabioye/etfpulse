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
    - Counter / state for OpenRouter cap is process-local (issue #12).

Graceful shutdown (issue #28 — resolved post-Stage-08): on lifespan exit
we (1) `pause()` the scheduler so no new jobs dispatch, (2) call
`shutdown(wait=True)` inside `asyncio.to_thread` so it waits for in-flight
jobs to finish without blocking the event loop, (3) cap the total wait at
`settings.scheduler_shutdown_grace_seconds` via `asyncio.wait_for`. On
timeout we log a warning and let event-loop teardown cancel orphans
(same fallback as before, but now only after the grace expires).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from etfpulse.adapters.sodex._http import SodexHttpClient
from etfpulse.adapters.sodex.perps_client import SodexPerpsClient
from etfpulse.adapters.sodex.spot_client import SodexSpotClient
from etfpulse.config import settings
from etfpulse.db import async_session
from etfpulse.models import ETFFlow, Signal, SignalStatus
from etfpulse.pipeline.ai_backfill import backfill_null_ai
from etfpulse.pipeline.delivery import fan_out_signal, send_pending_deliveries
from etfpulse.pipeline.reapers import (
    expire_overdue_orders,
    expire_overdue_signals,
    fail_stuck_deliveries,
)
from etfpulse.pipeline.reconcile import reconcile_open_orders
from etfpulse.pipeline.signal_builder import run_daily_cycle
from etfpulse.pipeline.symbols_refresh import refresh_sodex_symbols
from etfpulse.pipeline.track_record import evaluate_pending_outcomes

log = structlog.get_logger()

_DAILY_JOB_ID = "daily_cycle"
_INTRADAY_JOB_ID = "intraday_cycle"
_CATCHUP_JOB_ID = "catchup"
_DELIVERY_SEND_JOB_ID = "delivery_send"
_FAN_OUT_PENDING_JOB_ID = "fan_out_pending"
_OUTCOME_EVAL_JOB_ID = "outcome_eval"
_SIGNAL_EXPIRY_REAPER_JOB_ID = "signal_expiry_reaper"
_DELIVERY_REAPER_JOB_ID = "delivery_reaper"
# PR D.3 — execution-surface scheduled jobs.
_ORDER_EXPIRY_REAPER_JOB_ID = "order_expiry_reaper"
_ORDER_RECONCILE_JOB_ID = "order_reconcile"
_SYMBOLS_REFRESH_JOB_ID = "symbols_refresh"
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


async def _run_cycle_with_session(
    *, skip_if_no_new_data: bool = False, retry_null_ai: bool = False
) -> dict[str, Any] | None:
    """Production cycle wrapper — opens a session, commits on success.

    Called by APScheduler (daily cron + catch-up + intra-day interval) AND
    by the admin trigger route (`api/routes/admin.py`). Owns the transaction
    boundary; `run_daily_cycle` itself does not commit (D14).

    `skip_if_no_new_data` (Branch 3) is forwarded to `run_daily_cycle`:
    True for the intra-day interval (most ticks find nothing new and
    short-circuit), False for the daily cron + admin trigger + catch-up
    (always run detectors so the guarantees of "fires daily" + "operator
    can manually re-fire" are unconditional).

    `retry_null_ai` (Branch 6) — when True AND `settings.ai_backfill_enabled`,
    the wrapper calls `backfill_null_ai` AFTER `run_daily_cycle` returns
    (even when the cycle was skipped — backfill needs to run on no-new-data
    ticks to drain the AI-failed backlog). Only set by the intra-day
    partial; daily cron + admin trigger + catch-up leave it False so
    operators control the AI spend via the dedicated `/retry-ai` endpoint.

    Returns the summary dict on success, None on rollback. The scheduler
    discards the return value (logs only); the admin route surfaces it as
    JSON. Both share this function so the "demo works = scheduler works"
    invariant (D15) is literal, not aspirational.
    """
    async with async_session() as session:
        try:
            summary = await run_daily_cycle(session, skip_if_no_new_data=skip_if_no_new_data)

            # Branch 6 — auto-retry stranded AI signals. Bounded by
            # `ai_backfill_limit_per_tick` + `ai_backfill_max_age_hours`.
            # Runs in the SAME transaction as the cycle so either both
            # commit or both roll back. Summary is appended under
            # `ai_backfill` so log lines remain greppable separately.
            if retry_null_ai and settings.ai_backfill_enabled:
                backfill_summary = await backfill_null_ai(
                    session,
                    limit=settings.ai_backfill_limit_per_tick,
                    max_age_hours=settings.ai_backfill_max_age_hours or None,
                )
                summary["ai_backfill"] = {
                    k: v for k, v in backfill_summary.items() if k != "error_samples"
                }

            await session.commit()
            # Quieter log line when the intra-day skip-guard fires — the
            # signal_builder already emitted `daily_cycle_skipped_no_new_data`
            # with detail. Re-emitting the full summary on every no-op tick
            # would clutter logs without adding info.
            if not summary.get("skipped"):
                log.info("scheduled_cycle_committed", **summary)
            return summary
        except Exception as exc:
            await session.rollback()
            log.error(
                "scheduled_cycle_failed",
                error_type=type(exc).__name__,
                error=str(exc),
                exc_info=exc,
            )
            return None


async def _fan_out_pending_with_session() -> dict[str, int] | None:
    """Fan-out worker — materializes SignalDelivery rows for PENDING signals.

    Decoupled from `build_signal` per Decision D2 — signal_builder stays
    single-purpose (detect + AI + persist), and this separate scheduler job
    turns persisted signals into per-recipient delivery rows. Runs every
    `settings.delivery_worker_interval_seconds`.

    Query pre-filters out expired signals at the SELECT so we don't log
    thousands of "skip_expired" messages per tick. Per-signal SAVEPOINT
    around `fan_out_signal` means one misbehaving row doesn't roll back
    the whole batch (D13-style resilience).

    Does NOT commit per signal — outer commit at end covers everything
    that succeeded. Same transaction-boundary contract (D14/D18) as the
    other wrappers.
    """
    summary = {"processed": 0, "fanned_out": 0, "failed": 0}
    async with async_session() as session:
        try:
            stmt = select(Signal.id).where(
                Signal.status == SignalStatus.PENDING.value,
                or_(
                    Signal.expires_at.is_(None),
                    Signal.expires_at > datetime.now(UTC),
                ),
            )
            signal_ids = list((await session.execute(stmt)).scalars().all())

            for sid in signal_ids:
                try:
                    async with session.begin_nested():
                        count = await fan_out_signal(session, sid)
                    summary["processed"] += 1
                    summary["fanned_out"] += count
                except Exception as exc:
                    # Per-signal rollback via savepoint; batch continues.
                    log.warning(
                        "fan_out_pending_signal_failed",
                        signal_id=sid,
                        error_type=type(exc).__name__,
                        error=str(exc),
                    )
                    summary["failed"] += 1

            await session.commit()
            if summary["processed"] > 0 or summary["failed"] > 0:
                log.info("fan_out_pending_committed", **summary)
            return summary
        except Exception as exc:
            await session.rollback()
            log.error(
                "fan_out_pending_failed",
                error_type=type(exc).__name__,
                error=str(exc),
                exc_info=exc,
            )
            return None


async def _outcome_eval_with_session() -> dict[str, int] | None:
    """Outcome evaluator wrapper — scores aged signals into SignalOutcome rows.

    Stage 08-P3. Same transaction-boundary contract as the other wrappers
    (D14, D18): `evaluate_pending_outcomes` does NOT commit; this wrapper
    does. Returns None on rollback so an exception doesn't stop the
    APScheduler job from firing on subsequent ticks.

    Runs every `settings.outcome_eval_interval_seconds` (default 3600s = 1h).
    Per-signal resilience (one bad signal doesn't kill the cycle) lives
    inside `evaluate_pending_outcomes` itself; this wrapper only catches
    catastrophic failures the orchestrator can't handle in-loop
    (DB connection dropped mid-eval, asyncpg timeout, query-builder bug
    that raises before the per-signal try/except even runs).

    NOT gated on `is_bot_enabled` — the public track record exists
    independently of Telegram delivery (frontend `/track-record` + the
    home-page hit-rate tile both consume SignalOutcome regardless of
    whether the bot is wired up). Only `run_scheduler` gates this job.
    """
    async with async_session() as session:
        try:
            # Cap the per-tick batch via env (default 50). Bounds the
            # upstream klines-fetch volume so a fresh-deploy backlog can't
            # trip SoSoValue's 100 req/min cap. Backlog larger than the
            # cap gets drained over subsequent ticks.
            summary = await evaluate_pending_outcomes(
                session, limit=settings.outcome_eval_batch_limit
            )
            await session.commit()
            # Quiet on no-op ticks — most hourly runs find no new candidates.
            if summary.get("candidates", 0) > 0:
                log.info("outcome_eval_committed", **summary)
            return summary
        except Exception as exc:
            await session.rollback()
            log.error(
                "outcome_eval_failed",
                error_type=type(exc).__name__,
                error=str(exc),
                exc_info=exc,
            )
            return None


async def _send_with_session() -> dict[str, int] | None:
    """Delivery-send wrapper — drains PENDING SignalDelivery rows via Telegram.

    Same transaction-boundary contract as `_run_cycle_with_session` (D14,
    D18): `send_pending_deliveries` doesn't commit; this wrapper does. Also
    mirrors the returns-None-on-rollback pattern so exceptions don't kill
    the APScheduler job (which would then stop firing entirely).

    Runs every `settings.delivery_worker_interval_seconds` (default 30s).
    Per-delivery error handling (blocked user, chat-not-found, generic
    TelegramError) is entirely inside `send_pending_deliveries` — this
    wrapper only catches catastrophic failures (DB unreachable, bug in
    the worker itself).
    """
    async with async_session() as session:
        try:
            summary = await send_pending_deliveries(session)
            await session.commit()
            # Most ticks have nothing to send (total=0). Don't spam info logs
            # in that case; only log when at least one delivery was processed.
            if summary.get("total", 0) > 0:
                log.info("delivery_send_committed", **summary)
            return summary
        except Exception as exc:
            await session.rollback()
            log.error(
                "delivery_send_failed",
                error_type=type(exc).__name__,
                error=str(exc),
                exc_info=exc,
            )
            return None


async def _signal_expiry_reaper_with_session() -> dict[str, int] | None:
    """Issue #30 reaper — bulk-flips Signal.status PENDING/ALERTED → EXPIRED
    when expires_at is in the past.

    Same transaction-boundary contract (D14, D18) as the other wrappers:
    `expire_overdue_signals` does NOT commit; this wrapper does. Returns
    None on rollback so an exception doesn't stop the APScheduler job from
    firing on subsequent ticks.

    Runs every `settings.signal_expiry_reaper_interval_seconds`. NOT gated
    on `is_bot_enabled` — expiry is a DB-side concept that affects the
    web UI regardless of Telegram delivery.
    """
    async with async_session() as session:
        try:
            summary = await expire_overdue_signals(session)
            await session.commit()
            # Quiet on no-op ticks — most runs find nothing.
            if summary.get("expired", 0) > 0:
                log.info("signal_expiry_reaper_committed", **summary)
            return summary
        except Exception as exc:
            await session.rollback()
            log.error(
                "signal_expiry_reaper_failed",
                error_type=type(exc).__name__,
                error=str(exc),
                exc_info=exc,
            )
            return None


async def _delivery_reaper_with_session() -> dict[str, int] | None:
    """Issue #36 reaper — bulk-flips long-pending SignalDelivery rows to
    FAILED with a sentinel error_message.

    Same wrapper pattern as `_signal_expiry_reaper_with_session`. NOT
    gated on `is_bot_enabled`: even if the bot is disabled mid-flight,
    accumulated PENDING rows from the prior bot-enabled period should
    still be reaped to FAILED so the table doesn't carry stale state.
    """
    async with async_session() as session:
        try:
            summary = await fail_stuck_deliveries(session)
            await session.commit()
            if summary.get("stuck_failed", 0) > 0:
                log.info("delivery_reaper_committed", **summary)
            return summary
        except Exception as exc:
            await session.rollback()
            log.error(
                "delivery_reaper_failed",
                error_type=type(exc).__name__,
                error=str(exc),
                exc_info=exc,
            )
            return None


# ---------------------------------------------------------------------------
# PR D.3 — execution-surface job wrappers
# ---------------------------------------------------------------------------


def _build_sodex_http_client(*, base_url: str) -> SodexHttpClient:
    """Construct a SodexHttpClient with settings-derived parameters.

    Pulled into a single helper so the spot + perps build paths share
    the timeout/retry knobs and any future addition (proxy, custom
    transport in tests via override) lands in one place.
    """
    return SodexHttpClient(
        base_url=base_url,
        timeout_seconds=settings.sodex_http_timeout_seconds,
        retry_max_attempts=settings.sodex_http_retry_max_attempts,
        retry_base_seconds=settings.sodex_http_retry_base_seconds,
    )


def _build_sodex_clients() -> tuple[SodexSpotClient, SodexPerpsClient]:
    """Construct one spot + one perps client for the scheduler's lifetime.

    Each scheduled job that needs them imports them via closure from
    `start_scheduler`. The HTTP clients are shared across all jobs —
    httpx keepalive + the per-job DB session keep cost minimal.
    """
    spot_http = _build_sodex_http_client(base_url=settings.sodex_resolved_spot_base_url)
    perps_http = _build_sodex_http_client(base_url=settings.sodex_resolved_perps_base_url)
    return SodexSpotClient(spot_http), SodexPerpsClient(perps_http)


async def _order_expiry_reaper_with_session() -> dict[str, int] | None:
    """Bulk-expire orders past their nonce window. NOT HTTP — DB only,
    so we don't need the SoDEX clients here."""
    async with async_session() as session:
        try:
            summary = await expire_overdue_orders(session)
            await session.commit()
            if summary.get("expired", 0) > 0:
                log.info("orders_reaper_committed", **summary)
            return summary
        except Exception as exc:
            await session.rollback()
            log.error(
                "orders_reaper_failed",
                error_type=type(exc).__name__,
                error=str(exc),
                exc_info=exc,
            )
            return None


def _make_order_reconcile_wrapper(spot_client: SodexSpotClient, perps_client: SodexPerpsClient):
    """Factory — closes over the shared HTTP clients so APScheduler can
    call a zero-arg coroutine while still using the long-lived clients."""

    async def _wrapper() -> dict[str, int] | None:
        async with async_session() as session:
            try:
                summary = await reconcile_open_orders(
                    session,
                    spot_client=spot_client,
                    perps_client=perps_client,
                )
                await session.commit()
                # Always log (operators want visibility on the
                # active-users count); the function logs per-tick at
                # INFO too but we surface the commit boundary.
                log.debug("order_reconcile_committed", **summary)
                return summary
            except Exception as exc:
                await session.rollback()
                log.error(
                    "order_reconcile_failed",
                    error_type=type(exc).__name__,
                    error=str(exc),
                    exc_info=exc,
                )
                return None

    return _wrapper


def _make_symbols_refresh_wrapper(spot_client: SodexSpotClient, perps_client: SodexPerpsClient):
    async def _wrapper() -> dict[str, int] | None:
        async with async_session() as session:
            try:
                summary = await refresh_sodex_symbols(
                    session,
                    spot_client=spot_client,
                    perps_client=perps_client,
                )
                await session.commit()
                log.info("symbols_refresh_committed", **summary)
                return summary
            except Exception as exc:
                await session.rollback()
                log.error(
                    "symbols_refresh_failed",
                    error_type=type(exc).__name__,
                    error=str(exc),
                    exc_info=exc,
                )
                return None

    return _wrapper


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

    # Branch 3 — intra-day interval. Re-runs the cycle every
    # `intraday_cycle_interval_minutes`, short-circuiting when no new
    # ETF / news rows arrived since the last run. Closes the user-visible
    # gap where data published after the daily cron stayed invisible until
    # the next day. Disabled when the setting is 0.
    # `coalesce=True` so a slow tick collapses queued fires; `max_instances=1`
    # preserves D15.
    if settings.intraday_cycle_interval_minutes > 0:
        scheduler.add_job(
            # `partial` binds both kwargs cleanly — APScheduler introspects
            # the resulting callable for the job. `skip_if_no_new_data=True`
            # gates the cycle work; `retry_null_ai=True` enables Branch 6
            # AI-backfill on every tick (gated further by
            # `settings.ai_backfill_enabled` inside the wrapper).
            partial(_run_cycle_with_session, skip_if_no_new_data=True, retry_null_ai=True),
            trigger=IntervalTrigger(minutes=settings.intraday_cycle_interval_minutes),
            id=_INTRADAY_JOB_ID,
            max_instances=1,
            coalesce=True,
        )
        log.info(
            "scheduler_intraday_registered",
            interval_minutes=settings.intraday_cycle_interval_minutes,
            ai_backfill_enabled=settings.ai_backfill_enabled,
        )

    # Outcome evaluator (Stage 08-P3) — scores aged signals into SignalOutcome
    # rows for the public track record + dashboard hit-rate tile. Runs every
    # `outcome_eval_interval_seconds` (default 1h). NOT gated on the bot —
    # SignalOutcome powers the web UI regardless of Telegram delivery.
    # `max_instances=1` per D19. `coalesce=True` is set explicitly here
    # (the 30s delivery jobs deliberately don't) because outcome_eval is
    # the longest-running interval job: a backlog of unevaluated signals +
    # network-paced kline fetches can take minutes per tick. Coalescing
    # collapses queued fires into one rather than letting them stack and
    # serialise behind each other.
    scheduler.add_job(
        _outcome_eval_with_session,
        trigger=IntervalTrigger(seconds=settings.outcome_eval_interval_seconds),
        id=_OUTCOME_EVAL_JOB_ID,
        max_instances=1,
        coalesce=True,
    )

    # Reapers (issues #30 + #36). Not bot-gated — both touch DB state that
    # affects the web UI regardless of Telegram delivery (expired signals
    # render as "expired" on the frontend; stuck PENDING deliveries leak
    # into dashboards). Bulk-UPDATE only, so per-tick cost is one query
    # each. `coalesce=True` because both are idempotent — running once
    # after a missed window catches up cleanly.
    scheduler.add_job(
        _signal_expiry_reaper_with_session,
        trigger=IntervalTrigger(seconds=settings.signal_expiry_reaper_interval_seconds),
        id=_SIGNAL_EXPIRY_REAPER_JOB_ID,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        _delivery_reaper_with_session,
        trigger=IntervalTrigger(seconds=settings.delivery_reaper_interval_seconds),
        id=_DELIVERY_REAPER_JOB_ID,
        max_instances=1,
        coalesce=True,
    )

    # PR D.3 — execution-surface jobs.
    # NOT bot-gated: the order/position lifecycle affects the web UI
    # regardless of Telegram delivery.
    #   `order_expiry_reaper`: bulk-flip orders past their nonce window.
    #   `order_reconcile`: per-user fill detection + position drift.
    #   `symbols_refresh`: daily cron repopulating sodex_symbols cache.
    #
    # PR D.3.3 — `async with AsyncExitStack()` wraps the ENTIRE setup-
    # to-yield lifecycle so the SoDEX HTTP clients close on EVERY exit
    # path:
    #   - Normal yield-then-finally on lifespan shutdown.
    #   - Mid-enter failure (spot enters, perps __aenter__ raises).
    #   - Post-enter pre-yield failure (scheduler.add_job raises on
    #     duplicate job_id, scheduler.start raises on event-loop issue,
    #     etc.) — the D.3.2 narrow try/except didn't cover this.
    # The `async with` block's __aexit__ handles all three uniformly.
    sodex_spot_client, sodex_perps_client = _build_sodex_clients()
    async with AsyncExitStack() as sodex_clients_stack:
        await sodex_clients_stack.enter_async_context(sodex_spot_client)
        await sodex_clients_stack.enter_async_context(sodex_perps_client)
        app.state.sodex_spot_client = sodex_spot_client
        app.state.sodex_perps_client = sodex_perps_client

        # Register clearing of app.state to run BEFORE the client
        # `__aexit__`s on stack unwind (i.e., between scheduler-drain
        # and httpx-close). Rationale: admin routes that gate on
        # `app.state.sodex_*_client is not None` will see None
        # promptly and return 503, AVOIDING the race where a route
        # grabs a half-closed client mid-shutdown.
        #
        # AsyncExitStack is LIFO. `_clear_state` is pushed HERE (before
        # the scheduler-shutdown callback push at the bottom), so on
        # exit it runs SECOND — after `_shutdown_scheduler` (last
        # pushed → first unwound) and before the two client
        # `__aexit__` calls (registered earliest → unwound last).
        async def _clear_state() -> None:
            app.state.sodex_spot_client = None
            app.state.sodex_perps_client = None
            log.info("scheduler_sodex_clients_closed")

        sodex_clients_stack.push_async_callback(_clear_state)

        scheduler.add_job(
            _order_expiry_reaper_with_session,
            trigger=IntervalTrigger(seconds=settings.order_expiry_reaper_interval_seconds),
            id=_ORDER_EXPIRY_REAPER_JOB_ID,
            max_instances=1,
            coalesce=True,
        )
        scheduler.add_job(
            _make_order_reconcile_wrapper(sodex_spot_client, sodex_perps_client),
            trigger=IntervalTrigger(seconds=settings.order_reconcile_interval_seconds),
            id=_ORDER_RECONCILE_JOB_ID,
            max_instances=1,
            # coalesce=True: per-user HTTP fanout could overrun the 60s
            # interval at scale; queue-stacking would compound the problem.
            # See "scale follow-ups" in CLAUDE.md.
            coalesce=True,
        )
        scheduler.add_job(
            _make_symbols_refresh_wrapper(sodex_spot_client, sodex_perps_client),
            trigger=CronTrigger(
                hour=settings.symbols_refresh_cron_hour,
                minute=settings.symbols_refresh_cron_minute,
                timezone="UTC",
            ),
            id=_SYMBOLS_REFRESH_JOB_ID,
            max_instances=1,
            coalesce=True,
        )
        log.info(
            "scheduler_execution_jobs_registered",
            order_expiry_interval=settings.order_expiry_reaper_interval_seconds,
            order_reconcile_interval=settings.order_reconcile_interval_seconds,
            symbols_refresh_cron=(
                f"{settings.symbols_refresh_cron_hour:02d}:"
                f"{settings.symbols_refresh_cron_minute:02d} UTC"
            ),
        )

        # Delivery pipeline — two jobs gated on `is_bot_enabled`:
        #   `fan_out_pending`: turns PENDING Signals → SignalDelivery rows
        #   `delivery_send`:   drains PENDING SignalDelivery rows via Telegram
        # Both run every `delivery_worker_interval_seconds`. They're decoupled
        # (Decision D2) so signal_builder stays single-purpose; end-to-end
        # latency is 2× the interval in the worst case (fan-out tick + send tick).
        if settings.is_bot_enabled:
            scheduler.add_job(
                _fan_out_pending_with_session,
                trigger=IntervalTrigger(seconds=settings.delivery_worker_interval_seconds),
                id=_FAN_OUT_PENDING_JOB_ID,
                max_instances=1,
            )
            scheduler.add_job(
                _send_with_session,
                trigger=IntervalTrigger(seconds=settings.delivery_worker_interval_seconds),
                id=_DELIVERY_SEND_JOB_ID,
                max_instances=1,
            )
            log.info(
                "scheduler_delivery_jobs_registered",
                interval_seconds=settings.delivery_worker_interval_seconds,
                jobs=[_FAN_OUT_PENDING_JOB_ID, _DELIVERY_SEND_JOB_ID],
            )

        scheduler.start()
        app.state.scheduler = scheduler
        log.info(
            "scheduler_started",
            cron_hour=settings.scheduler_cron_hour,
            cron_minute=settings.scheduler_cron_minute,
            jobs=[j.id for j in scheduler.get_jobs()],
        )

        # Register graceful_shutdown ONLY after scheduler.start() has
        # succeeded. Pushing before start() would mean a failed start()
        # triggers stack unwind → _shutdown_scheduler → pause() →
        # apscheduler raises SchedulerNotRunningError on a never-started
        # scheduler. By registering only post-start, a pre-start failure
        # cleanly unwinds the client context managers + _clear_state
        # without touching the scheduler.
        #
        # LIFO order on the way out (post-yield AND on pre-yield
        # exception that lands after this push). `push_async_callback`
        # shares the stack with `enter_async_context`; everything
        # unwinds in strict last-in-first-out order:
        #   1. _shutdown_scheduler     (drains in-flight jobs)
        #   2. _clear_state            (None-out app.state references —
        #                              admin routes now see 503 promptly)
        #   3. perps_client.__aexit__  (aclose underlying httpx pool)
        #   4. spot_client.__aexit__   (aclose underlying httpx pool)
        async def _shutdown_scheduler() -> None:
            log.info("scheduler_shutdown_begin")
            await _graceful_shutdown(scheduler, settings.scheduler_shutdown_grace_seconds)

        sodex_clients_stack.push_async_callback(_shutdown_scheduler)

        yield


async def _graceful_shutdown(scheduler: AsyncIOScheduler, grace_seconds: int) -> None:
    """Drain in-flight jobs with a bounded wait, then shut down (issue #28).

    Sequence:
        1. `pause()` — stop dispatching new fires. Already-running jobs
           keep going.
        2. `await asyncio.wait_for(to_thread(shutdown, wait=True), grace)`
           — drain in-flight jobs in a worker thread so the event loop
           stays responsive, capped at `grace` seconds.
        3. On timeout, log + fall through. Event-loop teardown at process
           exit cancels any remaining orphan tasks (same end-state as the
           pre-#28 `wait=False` path, but only reached if drain exceeds
           the grace budget).

    `grace_seconds = 0` skips the bounded drain entirely and goes straight
    to `wait=False` — preserves the legacy behaviour for ops who want the
    old fast-shutdown.
    """
    if grace_seconds <= 0:
        scheduler.shutdown(wait=False)
        log.info("scheduler_shutdown_complete", reason="grace_disabled")
        return

    scheduler.pause()
    try:
        await asyncio.wait_for(
            asyncio.to_thread(scheduler.shutdown, wait=True),
            timeout=grace_seconds,
        )
        log.info("scheduler_shutdown_complete", reason="drained")
    except TimeoutError:
        log.warning(
            "scheduler_shutdown_timeout",
            grace_seconds=grace_seconds,
            note="orphan tasks will be cancelled by event-loop teardown",
        )
