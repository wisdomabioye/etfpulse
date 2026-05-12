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
    DeliveryTrace,
    DeliveryTraceRecipient,
    EvalOutcomesResponse,
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
from etfpulse.models import (
    ChannelType,
    DeliveryStatus,
    NotificationChannel,
    Signal,
    SignalDelivery,
    SignalStatus,
    TelegramGroup,
    User,
)
from etfpulse.pipeline import circuit_breaker
from etfpulse.pipeline.ai_backfill import backfill_null_ai
from etfpulse.pipeline.analysis import AI_PROMPT_VERSION
from etfpulse.pipeline.reapers import DELIVERY_REAPER_ERROR
from etfpulse.pipeline.scheduler import _run_cycle_with_session
from etfpulse.pipeline.track_record import evaluate_pending_outcomes

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
    "/signals/{signal_id}/delivery-trace",
    response_model=DeliveryTrace,
    dependencies=[Depends(require_admin_key)],
    include_in_schema=False,
)
async def get_signal_delivery_trace(
    signal_id: int,
    session: AsyncSession = Depends(get_db_session),
) -> DeliveryTrace:
    """Per-signal delivery diagnostic: who matched, who didn't, why.

    Answers the operational question "did this signal reach anyone, and
    if not, why?" without dropping to SQL. The thread that introduced
    Branch 5 spent ~4 SQL queries to figure out "the signal had confidence
    4 and both users had pref_min_confidence=6, so nobody matched." This
    endpoint condenses that diagnosis into one HTTP call.

    Iterates every (User, telegram NotificationChannel) pair and every
    TelegramGroup; for each, evaluates the same filters as
    `pipeline.delivery._match_users` / `_match_groups`. Joins any existing
    SignalDelivery row for the (signal, recipient) pair so the operator
    sees the delivery's status, attempt count, and error_message inline.

    Performance: queries are O(users + groups + deliveries), no JOIN
    explosion. At Phase 1 scale (a few hundred users + handful of groups)
    this is well under any latency concern. Index path is straightforward
    — sequential scans on small tables.

    404 if the signal doesn't exist (operator typo'd an id). No 503
    masking because the operator surface is private.
    """
    signal = await session.get(Signal, signal_id)
    if signal is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"signal {signal_id} not found",
        )

    # Index every existing delivery for this signal by recipient PK so we
    # can join inline without a second query per recipient.
    delivery_rows = (
        (await session.execute(select(SignalDelivery).where(SignalDelivery.signal_id == signal_id)))
        .scalars()
        .all()
    )
    deliveries_by_user_id: dict[int, SignalDelivery] = {
        d.user_id: d for d in delivery_rows if d.user_id is not None
    }
    deliveries_by_group_id: dict[int, SignalDelivery] = {
        d.group_id: d for d in delivery_rows if d.group_id is not None
    }

    # Aggregate counts up-front — operator's first glance needs to see
    # "delivered to N people" before drilling into the per-recipient list.
    delivery_count = len(delivery_rows)
    by_status: dict[str, int] = {}
    for d in delivery_rows:
        by_status[d.status] = by_status.get(d.status, 0) + 1

    # User × Telegram-channel pairs. A user with both a Telegram channel
    # AND e.g. an email channel would only appear here for the Telegram
    # row — non-Telegram channels can't receive signal alerts under the
    # current adapter surface.
    user_channel_rows = (
        await session.execute(
            select(User, NotificationChannel)
            .join(NotificationChannel, NotificationChannel.user_id == User.id)
            .where(NotificationChannel.channel_type == ChannelType.TELEGRAM.value)
        )
    ).all()

    group_rows = (await session.execute(select(TelegramGroup))).scalars().all()

    recipients: list[DeliveryTraceRecipient] = []
    matched_count = 0

    for user, channel in user_channel_rows:
        trace = _trace_user(user, channel, signal, deliveries_by_user_id.get(user.id))
        if trace.matched:
            matched_count += 1
        recipients.append(trace)

    for group in group_rows:
        trace = _trace_group(group, signal, deliveries_by_group_id.get(group.id))
        if trace.matched:
            matched_count += 1
        recipients.append(trace)

    return DeliveryTrace(
        signal_id=signal.id,
        signal_asset=signal.asset,
        signal_type=signal.signal_type,
        signal_confidence=signal.confidence,
        signal_status=signal.status,
        delivery_count=delivery_count,
        delivered_count=by_status.get(DeliveryStatus.DELIVERED.value, 0),
        pending_count=by_status.get(DeliveryStatus.PENDING.value, 0),
        failed_count=by_status.get(DeliveryStatus.FAILED.value, 0),
        skipped_count=by_status.get(DeliveryStatus.SKIPPED.value, 0),
        matched_count=matched_count,
        recipients=recipients,
    )


def _trace_user(
    user: User,
    channel: NotificationChannel,
    signal: Signal,
    delivery: SignalDelivery | None,
) -> DeliveryTraceRecipient:
    """Build a recipient trace for one (user, telegram-channel) pair.

    Filter evaluation order MUST match `pipeline.delivery._match_users`
    so the exclude_reason reflects the actual rule that excluded this
    target during the most recent fan-out. Asset and confidence checks
    use the helper to keep the rules in one place.
    """
    asset_match = _asset_matches(user.pref_assets, signal.asset)
    confidence_match = _confidence_matches(user.pref_min_confidence, signal.confidence)
    matched = (
        user.is_active
        and not user.pref_paused
        and channel.is_active
        and asset_match
        and confidence_match
    )
    reason = (
        None
        if matched
        else _first_failing_reason(
            user_active=user.is_active,
            user_paused=user.pref_paused,
            channel_active=channel.is_active,
            asset_match=asset_match,
            confidence_match=confidence_match,
            signal_confidence=signal.confidence,
            signal_asset=signal.asset,
            floor=user.pref_min_confidence,
            pref_assets=user.pref_assets,
        )
    )

    return DeliveryTraceRecipient(
        kind="user",
        target_id=user.id,
        target_label=channel.username or f"user#{user.id}",
        chat_id=channel.channel_identifier,
        target_active=user.is_active,
        target_paused=user.pref_paused,
        channel_active=channel.is_active,
        asset_match=asset_match,
        confidence_match=confidence_match,
        matched=matched,
        exclude_reason=reason,
        delivery_status=delivery.status if delivery else None,
        delivery_attempts=delivery.attempt_count if delivery else None,
        delivery_error=delivery.error_message if delivery else None,
    )


def _trace_group(
    group: TelegramGroup, signal: Signal, delivery: SignalDelivery | None
) -> DeliveryTraceRecipient:
    """Group analogue of `_trace_user` — same filter rules minus the
    channel join. Groups address Telegram directly via `chat_id`."""
    asset_match = _asset_matches(group.pref_assets, signal.asset)
    confidence_match = _confidence_matches(group.pref_min_confidence, signal.confidence)
    matched = group.is_active and not group.pref_paused and asset_match and confidence_match
    reason = (
        None
        if matched
        else _first_failing_reason(
            user_active=group.is_active,
            user_paused=group.pref_paused,
            channel_active=True,  # n/a for groups; always "pass" so it's
            # never the reason we report
            asset_match=asset_match,
            confidence_match=confidence_match,
            signal_confidence=signal.confidence,
            signal_asset=signal.asset,
            floor=group.pref_min_confidence,
            pref_assets=group.pref_assets,
            kind="group",
        )
    )

    return DeliveryTraceRecipient(
        kind="group",
        target_id=group.id,
        target_label=group.title or f"group#{group.id}",
        chat_id=group.chat_id,
        target_active=group.is_active,
        target_paused=group.pref_paused,
        channel_active=None,
        asset_match=asset_match,
        confidence_match=confidence_match,
        matched=matched,
        exclude_reason=reason,
        delivery_status=delivery.status if delivery else None,
        delivery_attempts=delivery.attempt_count if delivery else None,
        delivery_error=delivery.error_message if delivery else None,
    )


def _asset_matches(pref_assets: list[str], signal_asset: str) -> bool:
    """Pinned to `_match_users`'s `or_(cardinality == 0, contains([asset]))`
    rule — empty pref_assets means "all assets"."""
    return len(pref_assets) == 0 or signal_asset in pref_assets


def _confidence_matches(floor: int, signal_confidence: int | None) -> bool:
    """Pinned to `pref_min_confidence <= signal.confidence` — when the
    signal's confidence is NULL (AI failed), the comparison fails for
    everyone (fan-out's `_match_users` short-circuits on NULL confidence,
    so this is internally consistent)."""
    if signal_confidence is None:
        return False
    return floor <= signal_confidence


def _first_failing_reason(
    *,
    user_active: bool,
    user_paused: bool,
    channel_active: bool,
    asset_match: bool,
    confidence_match: bool,
    signal_confidence: int | None,
    signal_asset: str,
    floor: int,
    pref_assets: list[str],
    kind: str = "user",
) -> str:
    """First filter that the recipient failed, in the same evaluation
    order as `_match_users`. The single returned string is the most
    specific cause — operators want to see "asset mismatch" rather
    than a list of all violations."""
    target_word = "group" if kind == "group" else "user"
    if not user_active:
        return f"{target_word} inactive"
    if user_paused:
        return f"{target_word} paused"
    if kind == "user" and not channel_active:
        # Most-common silent-failure path: channel deactivated after a
        # prior Blocked / ChatNotFound. Make the cause explicit so an
        # operator doesn't go hunting for it.
        return (
            "Telegram channel inactive "
            "(likely auto-deactivated after a prior Blocked / ChatNotFound)"
        )
    if not asset_match:
        return f"signal asset {signal_asset!r} not in {target_word}'s pref_assets {pref_assets!r}"
    if not confidence_match:
        if signal_confidence is None:
            return "signal has no confidence (AI failed at build time)"
        return (
            f"signal confidence {signal_confidence} below {target_word}'s "
            f"pref_min_confidence ({floor})"
        )
    # All checks passed — caller shouldn't be asking for a reason.
    return "all filters passed (no exclude reason)"


@router.post(
    "/signals/eval-outcomes",
    response_model=EvalOutcomesResponse,
    dependencies=[Depends(require_admin_key)],
    include_in_schema=False,
)
async def eval_outcomes_now(
    limit: int = Query(default=20, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
) -> EvalOutcomesResponse:
    """Run the outcome evaluator on demand, capped at `limit` candidates.

    The scheduled job runs hourly and is bounded by
    `OUTCOME_EVAL_BATCH_LIMIT` (default 50). This endpoint is the
    operator-facing escape hatch for "drain my fresh-deploy backlog
    without waiting an hour" — same semantics, smaller default cap (20)
    so a click can't accidentally fire 100 sequential klines requests.

    Re-fire with the response's `remaining` value to drain a larger
    backlog: when `remaining == 0`, the queue is empty.

    Owns its own commit (consistent with other admin actions). Per-signal
    failures are absorbed by the helper's catch-and-continue loop and
    counted in `errored`; a catastrophic failure (DB drop, unexpected
    raise) propagates so the operator sees the actual exception rather
    than a silent partial commit.
    """
    log.info("admin_eval_outcomes_begin", limit=limit)
    summary = await evaluate_pending_outcomes(session, limit=limit)
    await session.commit()
    log.info(
        "admin_eval_outcomes_done",
        candidates=summary["candidates"],
        evaluated=summary["evaluated"],
        remaining=summary["remaining"],
    )
    return EvalOutcomesResponse(**summary)


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
    # would touch on its next tick. Branch 2 added `last_attempt_at IS
    # NULL` to the reaper so retrying rows (currently within the
    # exponential-backoff retry cycle managed by `send_pending_deliveries`)
    # aren't counted as stuck — they're actively being processed.
    stuck_stmt = select(func.count()).where(
        SignalDelivery.status == DeliveryStatus.PENDING.value,
        SignalDelivery.last_attempt_at.is_(None),
        SignalDelivery.created_at < stuck_cutoff,
    )
    deliveries_stuck_pending = int((await session.execute(stuck_stmt)).scalar_one())

    # --- All-time reaper-failure count ------------------------------------
    reaper_fail_stmt = select(func.count()).where(
        SignalDelivery.error_message == DELIVERY_REAPER_ERROR,
    )
    deliveries_reaper_failures = int((await session.execute(reaper_fail_stmt)).scalar_one())

    # --- Branch 5 — Signals ALERTED but with no SignalDelivery rows -------
    # Surfaces the "fanned out to nobody" case. Anti-JOIN (NOT EXISTS) is
    # the most index-friendly shape on Postgres — leverages
    # `ix_delivery_signal` on the right-hand subquery. Steady-state
    # depends on operator confidence-floor configs.
    zero_delivery_stmt = select(func.count()).where(
        Signal.status == SignalStatus.ALERTED.value,
        ~select(SignalDelivery.id).where(SignalDelivery.signal_id == Signal.id).exists(),
    )
    signals_alerted_with_zero_deliveries = int(
        (await session.execute(zero_delivery_stmt)).scalar_one()
    )

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

    # --- Active circuit breakers (issue #65) ------------------------------
    active_circuit_breakers = await circuit_breaker.count_active(session)

    return AdminMetrics(
        signal_status_counts=signal_status,
        delivery_status_counts=delivery_status,
        signals_overdue_unreaped=signals_overdue_unreaped,
        signals_null_confidence=signals_null_confidence,
        signals_null_confidence_alert=(
            signals_null_confidence > settings.signals_null_confidence_alert_threshold
        ),
        deliveries_stuck_pending=deliveries_stuck_pending,
        deliveries_reaper_failures=deliveries_reaper_failures,
        signals_alerted_with_zero_deliveries=signals_alerted_with_zero_deliveries,
        scheduler_jobs=scheduler_jobs,
        accepted_webhook_secrets=accepted_webhook_secrets,
        current_ai_prompt_version=AI_PROMPT_VERSION,
        signal_counts_by_prompt_version=signal_counts_by_prompt_version,
        active_circuit_breakers=active_circuit_breakers,
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
