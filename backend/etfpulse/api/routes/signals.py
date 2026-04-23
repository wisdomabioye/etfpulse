"""Public signal API — list (paginated + filtered) and detail.

No auth by design for Wave 1 (landing page is public). See open_issues.md #43.

Query shape:
    GET /api/signals?asset=BTC&signal_type=flow_anomaly&confidence_min=7
                    &include_expired=false&cursor=<iso>|<id>&limit=20
    GET /api/signals/{id}

Single SQL roundtrip for the list — signal + outcome_id (LEFT JOIN) +
alerted_to (correlated scalar subquery). LIMIT limit+1 detects "has more"
without a second COUNT query. Composite cursor `(created_at, id)` breaks
ties on same-millisecond inserts.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from etfpulse.api.deps import get_db_session
from etfpulse.api.schemas.signals import (
    PaginatedSignals,
    SignalDetail,
    SignalListItem,
    format_cursor,
    parse_cursor,
)
from etfpulse.models import Signal, SignalDelivery, SignalOutcome

log = structlog.get_logger()
router = APIRouter(prefix="/signals", tags=["signals"])


AssetQuery = Literal["BTC", "ETH"]
SignalTypeQuery = Literal["flow_anomaly", "magnitude", "acceleration", "divergence", "regime_shift"]


@router.get("", response_model=PaginatedSignals)
async def list_signals(
    asset: AssetQuery | None = Query(default=None),
    signal_type: SignalTypeQuery | None = Query(default=None),
    confidence_min: int | None = Query(default=None, ge=1, le=10),
    include_expired: bool = Query(default=False),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
) -> PaginatedSignals:
    """Reverse-chron cursor-paginated feed."""
    now = datetime.now(UTC)

    # Per-row `alerted_to` as a correlated scalar subquery — one query total.
    alerted_to_subq = (
        select(func.count())
        .select_from(SignalDelivery)
        .where(SignalDelivery.signal_id == Signal.id)
        .scalar_subquery()
        .label("alerted_to")
    )

    stmt = (
        select(Signal, SignalOutcome.id.label("outcome_id"), alerted_to_subq)
        .outerjoin(SignalOutcome, SignalOutcome.signal_id == Signal.id)
        .order_by(Signal.created_at.desc(), Signal.id.desc())
        # +1 to detect has-more without a separate COUNT query.
        .limit(limit + 1)
    )

    if asset is not None:
        stmt = stmt.where(Signal.asset == asset)
    if signal_type is not None:
        stmt = stmt.where(Signal.signal_type == signal_type)
    if confidence_min is not None:
        stmt = stmt.where(Signal.confidence >= confidence_min)
    if not include_expired:
        # NULL expires_at counts as "never expires" — include it.
        stmt = stmt.where(or_(Signal.expires_at.is_(None), Signal.expires_at > now))

    if cursor is not None:
        parsed = parse_cursor(cursor)
        if parsed is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="invalid cursor",
            )
        cursor_ts, cursor_id = parsed
        # Composite cursor — "(created_at, id) < (cursor_ts, cursor_id)"
        # expanded as OR (... AND ...). Correct tie-break on created_at
        # collisions from bulk fan-outs. Explicit form (not `tuple_`)
        # because SQLAlchemy's row-value comparison complains about mixing
        # raw Python values with column expressions at the type level.
        stmt = stmt.where(
            or_(
                Signal.created_at < cursor_ts,
                and_(Signal.created_at == cursor_ts, Signal.id < cursor_id),
            )
        )

    rows = (await session.execute(stmt)).all()

    has_more = len(rows) > limit
    page_rows = rows[:limit]

    items = [
        SignalListItem.from_row(row[0], outcome_id=row[1], alerted_to=row[2], now=now)
        for row in page_rows
    ]

    next_cursor: str | None = None
    if has_more and items:
        last = items[-1]
        next_cursor = format_cursor(last.created_at, last.id)

    return PaginatedSignals(items=items, next_cursor=next_cursor)


@router.get("/{signal_id}", response_model=SignalDetail)
async def get_signal(
    signal_id: int,
    session: AsyncSession = Depends(get_db_session),
) -> SignalDetail:
    """Full signal detail — trigger data + AI analysis + outcome (None until Stage 08)."""
    signal = (
        await session.execute(select(Signal).where(Signal.id == signal_id))
    ).scalar_one_or_none()
    if signal is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="signal not found")

    outcome = (
        await session.execute(select(SignalOutcome).where(SignalOutcome.signal_id == signal_id))
    ).scalar_one_or_none()

    alerted_to: int = (
        await session.execute(
            select(func.count())
            .select_from(SignalDelivery)
            .where(SignalDelivery.signal_id == signal_id)
        )
    ).scalar_one()

    return SignalDetail.from_row(
        signal, outcome=outcome, alerted_to=alerted_to, now=datetime.now(UTC)
    )
