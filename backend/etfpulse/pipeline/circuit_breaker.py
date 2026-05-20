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
    user_id: int | None = None,
) -> CircuitBreaker | None:
    """Idempotent INSERT for a new breaker activation.

    Returns the new row when one is inserted, or None when an unresolved
    row for the same `(trigger_type, user_id)` scope already exists.
    Scope dimension (PR D.3):
      - `user_id=None` → GLOBAL breaker. Idempotency key is
        `(trigger_type, user_id IS NULL)`.
      - `user_id=<int>` → per-user breaker. Idempotency key is
        `(trigger_type, user_id)`. Multiple users can hold the same
        trigger_type concurrently without de-duping each other; only
        the same user repeating the same trigger collapses.

    `trigger_type` must be one of `CircuitBreakerTrigger`'s values; the DB
    CHECK constraint rejects anything else with a clean IntegrityError.

    PR D.3.1 — race-safe via Postgres `INSERT ... ON CONFLICT DO NOTHING`
    against the partial unique index `uq_circuit_breakers_active_scope`
    (introduced by migration 8c61b9480195). The original D.3 used
    SELECT-then-INSERT, which raced under concurrent record() calls
    for the same scope → duplicate active rows. The single-statement
    upsert collapses the race to atomic. When the conflict path fires,
    the function falls through to `return None` like the original
    SELECT-miss path; callers' behaviour is unchanged.
    """
    from sqlalchemy import text
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    # ON CONFLICT must match the partial unique INDEX
    # `uq_circuit_breakers_active_scope` — Postgres matches by
    # `index_elements + index_where`, not `constraint=` (the partial
    # index has no SQL-standard constraint object).
    stmt = (
        pg_insert(CircuitBreaker)
        .values(trigger_type=trigger_type, user_id=user_id, details=details)
        .on_conflict_do_nothing(
            index_elements=[
                CircuitBreaker.trigger_type,
                text("COALESCE(user_id, -1)"),
            ],
            index_where=text("resolved_at IS NULL"),
        )
        .returning(CircuitBreaker.id)
    )
    result = await session.execute(stmt)
    new_id = result.scalar_one_or_none()
    if new_id is None:
        logger.debug(
            "circuit_breaker_record_noop",
            trigger_type=trigger_type,
            user_id=user_id,
            reason="already_unresolved",
        )
        return None

    # Load the freshly-inserted row so the caller has a fully-populated
    # CircuitBreaker (matching pre-D.3.1 contract).
    row = (
        await session.execute(select(CircuitBreaker).where(CircuitBreaker.id == new_id))
    ).scalar_one()
    logger.info(
        "circuit_breaker_recorded",
        id=row.id,
        trigger_type=trigger_type,
        user_id=user_id,
    )
    return row


async def find_active(
    session: AsyncSession,
    trigger_type: str,
    user_id: int | None = None,
) -> CircuitBreaker | None:
    """Return the currently-active breaker for the requested scope,
    or None if none exists.

    PR D.3.1 — added so the admin halt route can surface "who/when/why"
    on the idempotent-noop path (record() returning None doesn't tell
    the operator which existing breaker is blocking).
    """
    q = select(CircuitBreaker).where(
        CircuitBreaker.trigger_type == trigger_type,
        CircuitBreaker.resolved_at.is_(None),
    )
    if user_id is None:
        q = q.where(CircuitBreaker.user_id.is_(None))
    else:
        q = q.where(CircuitBreaker.user_id == user_id)
    q = q.order_by(CircuitBreaker.triggered_at.desc()).limit(1)
    return (await session.execute(q)).scalar_one_or_none()


async def resolve(
    session: AsyncSession,
    trigger_type: str,
    resolved_by: str,
    user_id: int | None = None,
) -> int:
    """Flip unresolved rows for the given scope to resolved.

    Scope dimension (PR D.3):
      - `user_id=None` → resolves GLOBAL breakers only (rows with
        `user_id IS NULL`). Does NOT clear per-user breakers.
      - `user_id=<int>` → resolves only that user's breakers.

    Resolving a global breaker does not implicitly resolve any per-user
    breakers — they're independent dimensions. Callers wanting "fully
    clear this user's halt state" must resolve both global (if relevant)
    and per-user explicitly.

    Returns the number of rows updated. Zero is valid. `resolved_by` is
    free-form — convention: `"auto"` for self-healing, the operator's
    identifier for manual clears.
    """

    stmt = update(CircuitBreaker).where(
        CircuitBreaker.trigger_type == trigger_type,
        CircuitBreaker.resolved_at.is_(None),
    )
    if user_id is None:
        stmt = stmt.where(CircuitBreaker.user_id.is_(None))
    else:
        stmt = stmt.where(CircuitBreaker.user_id == user_id)
    stmt = stmt.values(resolved_at=datetime.now(UTC), resolved_by=resolved_by)

    # `rowcount` lives on CursorResult; AsyncSession.execute's static return
    # type doesn't expose it. Same cast pattern as `pipeline/reapers.py`.
    result = cast(CursorResult[Any], await session.execute(stmt))
    rowcount = result.rowcount or 0
    if rowcount:
        logger.info(
            "circuit_breaker_resolved",
            trigger_type=trigger_type,
            user_id=user_id,
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
