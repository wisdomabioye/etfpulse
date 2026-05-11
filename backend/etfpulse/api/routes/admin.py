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

import secrets as secrets_module
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from telegram.error import TelegramError

from etfpulse.adapters.telegram import telegram_client
from etfpulse.api.deps import get_db_session, require_admin_key
from etfpulse.api.schemas.admin import (
    AdminMetrics,
    DeliveryStatusCounts,
    RetryAiErrorSample,
    RetryAiResponse,
    SchedulerJobInfo,
    SignalStatusCounts,
)
from etfpulse.api.schemas.telegram_admin import (
    RotateWebhookSecretRequest,
    RotateWebhookSecretResponse,
)
from etfpulse.bot.constants import ALLOWED_UPDATES
from etfpulse.config import settings
from etfpulse.models import DeliveryStatus, Signal, SignalDelivery, SignalStatus
from etfpulse.pipeline.ai_backfill import backfill_null_ai
from etfpulse.pipeline.analysis import AI_PROMPT_VERSION
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


@router.post(
    "/signals/retry-ai",
    response_model=RetryAiResponse,
    dependencies=[Depends(require_admin_key)],
    include_in_schema=False,
)
async def retry_ai_for_null_signals(
    limit: int = Query(default=10, ge=1, le=50),
    session: AsyncSession = Depends(get_db_session),
) -> RetryAiResponse:
    """Re-run AI enrichment on Signals where `ai_analysis IS NULL`.

    `signal_builder.build_signal` only enriches NEWLY-inserted rows (D12),
    so signals stranded by an earlier OpenRouter outage (insufficient
    credits, schema drift, daily cap hit) are never retried by the daily
    cycle. This endpoint is the operator-facing escape hatch.

    `limit` caps OpenRouter spend per click — re-fire the action multiple
    times to drain a backlog. Idempotent: a second call with `updated > 0`
    on the first call will skip those now-enriched rows. Defaults to 10
    (a safe single-click cost) and is bounded at 50.

    The transaction is committed inside this handler so a partial-success
    batch persists what it managed to enrich (consistent with the
    `_run_cycle_with_session` wrapper pattern — admin actions own their
    own commits).
    """
    log.info("admin_retry_ai_begin", limit=limit)
    summary = await backfill_null_ai(session, limit=limit)
    await session.commit()
    log.info(
        "admin_retry_ai_done",
        scanned=summary["scanned"],
        updated=summary["updated"],
        failed=summary["failed"],
    )
    return RetryAiResponse(
        scanned=summary["scanned"],
        updated=summary["updated"],
        failed=summary["failed"],
        error_samples=[RetryAiErrorSample(**s) for s in summary["error_samples"]],
    )


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

    # --- Webhook secret-set size (issue #40 stuck-rotation visibility) ----
    # None when bot is disabled (no state to inspect). 1 in steady state;
    # 2+ means a widen-then-shrink rotation didn't complete.
    secrets_set = getattr(request.app.state, "telegram_webhook_secrets", None)
    accepted_webhook_secrets = len(secrets_set) if secrets_set is not None else None

    # --- Prompt-version distribution (issue #32) --------------------------
    # GROUP BY ai_prompt_version. Sorted by count DESC so the dominant
    # cohort renders first in the dashboard. Empty DB → {}.
    pv_rows = (
        await session.execute(
            select(Signal.ai_prompt_version, func.count())
            .group_by(Signal.ai_prompt_version)
            # Secondary sort by version string for deterministic display
            # when two cohorts have equal counts (v2=50 and v3=50 would
            # otherwise flip between renders).
            .order_by(func.count().desc(), Signal.ai_prompt_version)
        )
    ).all()
    signal_counts_by_prompt_version = {row[0]: row[1] for row in pv_rows}

    return AdminMetrics(
        signal_status_counts=signal_status,
        delivery_status_counts=delivery_status,
        signals_overdue_unreaped=signals_overdue_unreaped,
        signals_null_confidence=signals_null_confidence,
        deliveries_stuck_pending=deliveries_stuck_pending,
        deliveries_reaper_failures=deliveries_reaper_failures,
        scheduler_jobs=scheduler_jobs,
        accepted_webhook_secrets=accepted_webhook_secrets,
        current_ai_prompt_version=AI_PROMPT_VERSION,
        signal_counts_by_prompt_version=signal_counts_by_prompt_version,
    )


@router.post(
    "/telegram/rotate-webhook-secret",
    response_model=RotateWebhookSecretResponse,
    dependencies=[Depends(require_admin_key)],
    include_in_schema=False,
)
async def rotate_webhook_secret(
    request: Request,
    body: RotateWebhookSecretRequest | None = None,
) -> RotateWebhookSecretResponse:
    """Rotate the Telegram webhook secret WITHOUT a container restart (issue #40).

    Race-free protocol:
        1. Widen `app.state.telegram_webhook_secrets` to {old, new} BEFORE
           calling Telegram. Any in-flight webhook POST during the rotation
           (old-secret-signed) still verifies. Any post-rotation POST
           (new-secret-signed) also verifies.
        2. Call `set_webhook(secret_token=new)`. Telegram now signs future
           webhooks with the new secret.
        3. On success, shrink the set to just {new}. The old secret is
           rejected from this point forward — that's the actual rotation.
        4. On failure, revert the set to {old}. Telegram is still signing
           with the old secret (set_webhook never landed), so the bot
           keeps working with no operator-visible disruption.

    Body is optional — omit to have the server generate a fresh secret;
    supply `secret` if the operator wants to coordinate the rotation with
    an env var update (and needs to know the value in advance).

    The response is a one-time disclosure — the secret cannot be retrieved
    again. The `note` field reminds the operator to update the deploy
    env var so the new value survives a container restart.

    Pre-conditions:
        - Bot must be enabled (`app.state.bot_application` attached).
            Otherwise there's nothing to rotate; return 503.
    """
    if getattr(request.app.state, "bot_application", None) is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="bot disabled — nothing to rotate",
        )

    # Serialise concurrent rotations. The lock is held across the full
    # widen → set_webhook → shrink sequence so a second concurrent caller
    # waits its turn rather than racing on app.state.
    lock = getattr(request.app.state, "telegram_webhook_rotate_lock", None)
    if lock is None:
        # Defensive — lifespan should have created it. Without serialisation
        # we can't guarantee atomicity, so refuse rather than corrupt state.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="rotation lock uninitialised",
        )

    async with lock:
        return await _do_rotate(request, body)


async def _do_rotate(
    request: Request, body: RotateWebhookSecretRequest | None
) -> RotateWebhookSecretResponse:
    """Inner rotation body — runs under `telegram_webhook_rotate_lock`."""
    old_secrets: set[str] = getattr(request.app.state, "telegram_webhook_secrets", set())
    if not old_secrets:
        # Defensive — lifespan should have initialised this. If it didn't,
        # we have no "old" to widen around, so refuse.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="webhook secret state uninitialised",
        )

    new_secret = (body.secret if body else None) or secrets_module.token_hex(32)
    webhook_url = getattr(request.app.state, "telegram_webhook_url", None)
    if webhook_url is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="webhook url unavailable",
        )

    # Step 1 — widen.
    request.app.state.telegram_webhook_secrets = old_secrets | {new_secret}
    log.info("webhook_rotate_begin", widened_to=len(old_secrets) + 1)

    # Step 2 — push to Telegram.
    try:
        await telegram_client.set_webhook(
            url=webhook_url,
            secret_token=new_secret,
            allowed_updates=ALLOWED_UPDATES,
        )
    except TelegramError as exc:
        # Step 4 — revert. Old secret is still what Telegram signs with.
        request.app.state.telegram_webhook_secrets = old_secrets
        log.warning(
            "webhook_rotate_failed",
            error_type=type(exc).__name__,
            error=str(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="set_webhook failed — rotation reverted, old secret still active",
        ) from exc

    # Step 3 — shrink. Telegram now signs with new; old is dead.
    request.app.state.telegram_webhook_secrets = {new_secret}
    log.info("webhook_rotate_complete")

    return RotateWebhookSecretResponse(secret=new_secret)
