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
from etfpulse.models import (
    DeliveryStatus,
    Order,
    OrderStatus,
    Signal,
    SignalDelivery,
    SignalStatus,
)

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
    """Bulk-update SignalDelivery rows that have been PENDING too long
    WITHOUT ever being attempted.

    Branch 2 narrowed the WHERE clause: `last_attempt_at IS NULL`. Rows
    that have been attempted at least once are in an active retry cycle
    managed by `send_pending_deliveries` (PENDING → retry-with-backoff
    → either DELIVERED or FAILED at the cap). The reaper must not race
    them — flipping a row mid-retry to FAILED would terminate retries
    that haven't reached their attempt budget.

    The reaper now only catches the "worker never picked it up" failure
    mode: scheduler halted, bot disabled mid-flight, fan-out wrote rows
    that no worker drained. Those rows have a NULL `last_attempt_at`,
    age past `delivery_pending_max_age_seconds`, and need a terminal
    state so dashboards see why they didn't deliver.

    Targets:
        - status = 'pending'
        - last_attempt_at IS NULL (never picked up by the send worker)
        - created_at < now - delivery_pending_max_age_seconds

    Index `ix_delivery_pending` covers the status predicate; the rest is
    a small extra scan within that subset. Returns `{stuck_failed: N}`.
    """
    now = datetime.now(UTC)
    cutoff = now - timedelta(seconds=settings.delivery_pending_max_age_seconds)
    stmt = (
        update(SignalDelivery)
        .where(
            SignalDelivery.status == DeliveryStatus.PENDING.value,
            SignalDelivery.last_attempt_at.is_(None),
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


# PR D.3 — order statuses that should be terminalised on nonce-window
# expiry. PARTIALLY_FILLED is included: the filled portion's Position
# is already in our DB; transitioning Order to EXPIRED doesn't unwind
# the position. The audit trail (filled_size on Order, Position row) is
# preserved.
# PR P1-fix.REAP-2 — the reaper targets ONLY orders the venue never
# accepted: PENDING (signed-or-not, never sent) and SUBMITTED (sent,
# ACK never persisted — crash window). Their signed payload genuinely
# can no longer be submitted once the nonce window passes, so they're
# dead and should be EXPIRED.
#
# ACKED / PARTIALLY_FILLED are DELIBERATELY EXCLUDED: such an order is
# already accepted and LIVE on the venue. The nonce is spent — the order
# never needs re-submission — and its lifetime is governed by the venue
# (a resting GTC limit) or its trigger (a resting stop), NOT the 24h
# nonce. EXPIRING such a row locally would (a) lie about a venue-live
# order, (b) drop it out of `_RECONCILABLE_STATUSES` so a later venue
# fill is never folded into the Position (silent drift), and (c) break
# GTC-limit "good till cancelled" semantics. This supersedes the
# narrower REAP-1 carve-out (which exempted only *conditional* venue-
# live orders) — the correct rule applies to ALL venue-live orders.
_OVERDUE_ORDER_STATUSES: frozenset[str] = frozenset(
    {
        OrderStatus.PENDING.value,
        OrderStatus.SUBMITTED.value,
    }
)


async def expire_overdue_orders(session: AsyncSession) -> dict[str, int]:
    """Bulk-update never-accepted orders past their EIP-712 nonce window.

    The SoDEX gateway enforces a 1-day nonce window (api.md). Once
    `nonce_expires_at < now`, the signed payload can no longer be
    submitted — even retry is structurally impossible without
    re-signing. This reaper terminalises orders that were never accepted
    by the venue and so can never be submitted again.

    Targets only rows where:
        - `status IN (pending, submitted)`  — never venue-accepted
        - `nonce_expires_at IS NOT NULL`
        - `nonce_expires_at < now()`

    **PR P1-fix.REAP-2 (supersedes REAP-1):** ACKED / PARTIALLY_FILLED
    orders are NOT reaped — they're live on the venue, the nonce is
    spent, and a local EXPIRE would create DB/venue drift (the row falls
    out of `_RECONCILABLE_STATUSES`, so a later venue fill is never
    detected) and break GTC-limit resting + resting-stop semantics. Such
    an order leaves the active set only via a real terminal transition
    (fill / cancel / reject) detected by reconcile or the cancel flow.

    The partial index `ix_orders_expires` covers a superset of this
    predicate (it indexes acked/partially_filled too, from before
    REAP-2); the planner applies the narrowed status filter as a
    residual. Still index-eligible; no migration needed.

    Idempotent — re-runs are no-ops because once a row is EXPIRED it
    no longer matches the predicate.

    D14: does NOT commit. Returns `{expired: N}`.
    """
    now = datetime.now(UTC)
    stmt = (
        update(Order)
        .where(
            Order.status.in_(_OVERDUE_ORDER_STATUSES),
            Order.nonce_expires_at.is_not(None),
            Order.nonce_expires_at < now,
        )
        .values(status=OrderStatus.EXPIRED.value)
    )
    result = cast(CursorResult[Any], await session.execute(stmt))
    summary = {"expired": result.rowcount or 0}
    if summary["expired"] > 0:
        log.info("orders_reaper_expired", **summary)
    return summary
