"""Periodic cleanup jobs — separate concern from signal building / delivery.

Two reapers, both bulk-UPDATE only (no row-by-row processing):

    1. `expire_overdue_signals` — issue #30. Flips Signal.status to EXPIRED
       when `expires_at < now`. Signals with NULL expires_at (AI failure at
       build time) are intentionally NOT touched — their horizon is
       indeterminate, so auto-expiring them would be guesswork. A separate
       policy on AI-failed signals can be added later.

    2. `fail_stuck_deliveries` — issue #36. Flips long-pending
       SignalDelivery rows to FAILED with a sentinel error_message. "Long"
       is `settings.delivery_pending_max_age_seconds` (default 10 min ≈ 20
       send-worker ticks). FAILED rows are NOT touched — they have a
       definite terminal state and serve as audit trail.

Both functions follow D14 (do NOT commit; caller wraps the transaction)
and return a small summary dict — same convention as
`pipeline.delivery.send_pending_deliveries` and
`pipeline.track_record.evaluate_pending_outcomes`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

import structlog
from sqlalchemy import CursorResult, update
from sqlalchemy.ext.asyncio import AsyncSession

from etfpulse.config import settings
from etfpulse.models import DeliveryStatus, Signal, SignalDelivery, SignalStatus

log = structlog.get_logger()

# Sentinel string written to SignalDelivery.error_message when the reaper
# flips a stuck PENDING row to FAILED. Pinned here so dashboards / log
# searches have one stable string to grep for.
DELIVERY_REAPER_ERROR = "reaper: pending exceeded max age"


async def expire_overdue_signals(session: AsyncSession) -> dict[str, int]:
    """Bulk-update Signals whose `expires_at` is in the past.

    Targets only rows where:
        - status IS NOT 'expired' (idempotent — re-runs are no-ops)
        - expires_at IS NOT NULL  (AI-failed signals excluded by design)
        - expires_at < now()

    The unique index `ix_signals_expires` covers the predicate path.
    Returns `{expired: N}` where N is rowcount affected.
    """
    now = datetime.now(UTC)
    stmt = (
        update(Signal)
        .where(
            Signal.status != SignalStatus.EXPIRED.value,
            Signal.expires_at.is_not(None),
            Signal.expires_at < now,
        )
        .values(status=SignalStatus.EXPIRED.value)
    )
    # `rowcount` exists on CursorResult (the runtime type returned by an
    # UPDATE) but isn't on the Result base type mypy infers from
    # AsyncSession.execute. Cast the whole result so the attribute access
    # type-checks rather than wrapping every read.
    result = cast(CursorResult[Any], await session.execute(stmt))
    summary = {"expired": result.rowcount or 0}
    if summary["expired"] > 0:
        log.info("signals_reaper_expired", **summary)
    return summary


async def fail_stuck_deliveries(session: AsyncSession) -> dict[str, int]:
    """Bulk-update SignalDelivery rows that have been PENDING too long.

    Targets only rows where:
        - status = 'pending'  (DELIVERED / FAILED / SKIPPED untouched)
        - created_at < now - delivery_pending_max_age_seconds

    The send worker's "no retry" policy (D4) means a row should reach a
    terminal state on its first send attempt. A row stuck PENDING means
    the worker never picked it up (bot disabled mid-flight, scheduler
    halted, etc). Flipping to FAILED with a sentinel `error_message` makes
    the cause visible without losing the row.

    Index `ix_delivery_pending` covers the status predicate; the
    `created_at` filter is a small extra scan within that subset.
    Returns `{stuck_failed: N}`.
    """
    now = datetime.now(UTC)
    cutoff = now - timedelta(seconds=settings.delivery_pending_max_age_seconds)
    stmt = (
        update(SignalDelivery)
        .where(
            SignalDelivery.status == DeliveryStatus.PENDING.value,
            SignalDelivery.created_at < cutoff,
        )
        .values(
            status=DeliveryStatus.FAILED.value,
            error_message=DELIVERY_REAPER_ERROR,
        )
    )
    result = cast(CursorResult[Any], await session.execute(stmt))
    summary = {"stuck_failed": result.rowcount or 0}
    if summary["stuck_failed"] > 0:
        log.info(
            "delivery_reaper_failed_stuck",
            cutoff=cutoff.isoformat(),
            **summary,
        )
    return summary
