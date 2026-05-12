"""CircuitBreaker persistence (issue #65).

Two-state lifecycle per `(trigger_type)`:

  - `record(trigger_type, details)` → INSERT a row with `resolved_at=NULL`,
    *iff* no unresolved row for the same `trigger_type` already exists. The
    no-op-on-duplicate path makes the function safe to call on every
    risk-controller tick — Stage 09's design has the executor probe risk
    state ~once per Execute attempt, and we don't want one breaker event
    to spawn N audit rows.
  - `resolve(trigger_type, resolved_by)` → flip `resolved_at = now()`,
    `resolved_by = <operator | "auto">` on every currently-unresolved row
    for the trigger_type. Idempotent: zero unresolved rows is a no-op,
    multiple unresolved rows (shouldn't happen under record's invariant
    but tolerate it) all get the same resolution.

Module is D14-compliant: neither function commits — the caller wraps the
transaction. The risk controller in Stage 09 calls these alongside the
order-placement logic, sharing the same DB transaction so a breaker
record persists IFF the decision that triggered it also persists.

`count_active(session) → int` is the read used by `/api/admin/metrics`
to surface "any breakers stuck unresolved" without dumping the full row
set into the response.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import structlog
from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from etfpulse.models.regime import CircuitBreaker

logger = structlog.get_logger(__name__)


async def record(
    session: AsyncSession,
    trigger_type: str,
    details: dict[str, Any] | None = None,
) -> CircuitBreaker | None:
    """Idempotent INSERT for a new breaker activation.

    Returns the new row when one is inserted, or None when an unresolved
    row for the same `trigger_type` already exists (the caller doesn't
    need to distinguish "first activation now" from "still active from
    before" — both states mean "breaker is currently tripped").

    `trigger_type` must be one of `CircuitBreakerTrigger`'s values; the DB
    CHECK constraint (#71) rejects anything else with a clean IntegrityError.
    """

    existing = await session.execute(
        select(CircuitBreaker.id)
        .where(
            CircuitBreaker.trigger_type == trigger_type,
            CircuitBreaker.resolved_at.is_(None),
        )
        .limit(1)
    )
    if existing.scalar_one_or_none() is not None:
        logger.debug(
            "circuit_breaker_record_noop",
            trigger_type=trigger_type,
            reason="already_unresolved",
        )
        return None

    row = CircuitBreaker(trigger_type=trigger_type, details=details)
    session.add(row)
    await session.flush()  # populate row.id without committing
    logger.info(
        "circuit_breaker_recorded",
        id=row.id,
        trigger_type=trigger_type,
    )
    return row


async def resolve(
    session: AsyncSession,
    trigger_type: str,
    resolved_by: str,
) -> int:
    """Flip every unresolved row for `trigger_type` to resolved.

    Returns the number of rows updated. Zero is a valid result (the
    breaker was already resolved or never recorded). `resolved_by` is
    free-form — convention: `"auto"` for self-healing, the operator's
    identifier for manual clears.
    """

    stmt = (
        update(CircuitBreaker)
        .where(
            CircuitBreaker.trigger_type == trigger_type,
            CircuitBreaker.resolved_at.is_(None),
        )
        .values(resolved_at=datetime.now(UTC), resolved_by=resolved_by)
    )
    # `rowcount` lives on CursorResult; AsyncSession.execute's static return
    # type doesn't expose it. Same cast pattern as `pipeline/reapers.py`.
    result = cast(CursorResult[Any], await session.execute(stmt))
    rowcount = result.rowcount or 0
    if rowcount:
        logger.info(
            "circuit_breaker_resolved",
            trigger_type=trigger_type,
            resolved_by=resolved_by,
            rowcount=rowcount,
        )
    return rowcount


async def count_active(session: AsyncSession) -> int:
    """Number of breakers with `resolved_at IS NULL`. Steady-state value is 0.

    Persistent non-zero = a trigger fired and was never cleared (either
    the auto-healing path is broken or operator intervention is overdue).
    """

    stmt = select(func.count()).where(CircuitBreaker.resolved_at.is_(None))
    return int((await session.execute(stmt)).scalar_one())
