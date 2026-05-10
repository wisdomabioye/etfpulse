"""Public signal API — list (paginated + filtered) and detail.

No auth by design for Phase 1 (landing page is public). See open_issues.md #43.

Query shape:
    GET /api/signals?asset=BTC&signal_type=flow_anomaly&confidence_min=7
                    &include_expired=false&sort=newest|oldest
                    &cursor=<iso>|<id>&page=N&limit=20
    GET /api/signals/{id}

Pagination has TWO modes (mutually exclusive — `page` wins if both supplied):
  - **Cursor** (default): keyset using `(created_at, id)`. Returns
    `next_cursor` and probes "has more" with a `LIMIT limit+1` row. Best for
    infinite-scroll feeds; stable under concurrent inserts.
  - **Page** (when `?page=N` is supplied): offset-based slice. Returns the
    1-based `page` plus `total_pages` for numbered-pager UIs.

Per-row data is one JOINed SELECT (signal + outcome_id LEFT JOIN +
alerted_to scalar subquery). On top of that, a single `COUNT(*)` runs over
the same WHERE set on every list call so `total` is always populated; this
makes the response shape uniform across both modes at the cost of one cheap
extra roundtrip. Revisit if signals grow past ~1M rows.

Cursor semantics are sort-direction dependent. For `newest` (DESC) the
comparison is `(created_at, id) < cursor`; for `oldest` (ASC) it is `>`.
Switching sort invalidates any existing cursor — clients restart from
page 1 on sort change (TanStack invalidates the cached query automatically
when the sort value enters the queryKey).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal, TypeVar

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from etfpulse.api.deps import get_db_session
from etfpulse.api.schemas.signals import (
    AssetLiteral,
    PaginatedSignals,
    SignalDetail,
    SignalListItem,
    SignalTypeLiteral,
    format_cursor,
    parse_cursor,
)
from etfpulse.models import Signal, SignalDelivery, SignalOutcome

# Generic so `_apply_filters` returns the same Select shape it accepts —
# row-shape SELECTs and COUNT(*) SELECTs reuse the same WHERE-builder.
_S = TypeVar("_S", bound=Select[Any])

log = structlog.get_logger()
router = APIRouter(prefix="/signals", tags=["signals"])


SortQuery = Literal["newest", "oldest"]


@router.get("", response_model=PaginatedSignals)
async def list_signals(
    asset: AssetLiteral | None = Query(default=None),
    signal_type: SignalTypeLiteral | None = Query(default=None),
    confidence_min: int | None = Query(default=None, ge=1, le=10),
    include_expired: bool = Query(default=False),
    sort: SortQuery = Query(default="newest"),
    cursor: str | None = Query(default=None),
    page: int | None = Query(default=None, ge=1),
    limit: int = Query(default=20, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
) -> PaginatedSignals:
    """List signals with cursor OR page pagination.

    `sort` flips ORDER BY + cursor comparison direction.

    Pagination modes (mutually exclusive — `page` wins if both supplied):
      - `cursor` → keyset, returns `next_cursor` for "load more" UIs.
      - `page` (1-based) → offset, returns `total / page / total_pages`
        for numbered-pager UIs. A separate COUNT query runs alongside the
        page slice. Both are filled regardless of mode so clients can pick.
    """
    now = datetime.now(UTC)

    # WHERE-clause builder shared by the row query AND the COUNT query so
    # they cannot drift on filter logic. Generic in `_S` so SQLAlchemy keeps
    # the input row-shape on the way out.
    def _apply_filters(q: _S) -> _S:
        if asset is not None:
            q = q.where(Signal.asset == asset)
        if signal_type is not None:
            q = q.where(Signal.signal_type == signal_type)
        if confidence_min is not None:
            q = q.where(Signal.confidence >= confidence_min)
        if not include_expired:
            # NULL expires_at counts as "never expires" — include it.
            q = q.where(or_(Signal.expires_at.is_(None), Signal.expires_at > now))
        return q

    # Per-row `alerted_to` as a correlated scalar subquery — one query total.
    alerted_to_subq = (
        select(func.count())
        .select_from(SignalDelivery)
        .where(SignalDelivery.signal_id == Signal.id)
        .scalar_subquery()
        .label("alerted_to")
    )

    asc_order = sort == "oldest"

    stmt = (
        select(Signal, SignalOutcome.id.label("outcome_id"), alerted_to_subq)
        .outerjoin(SignalOutcome, SignalOutcome.signal_id == Signal.id)
        .order_by(
            Signal.created_at.asc() if asc_order else Signal.created_at.desc(),
            Signal.id.asc() if asc_order else Signal.id.desc(),
        )
    )
    stmt = _apply_filters(stmt)

    # Total count — single COUNT(*) over the same WHERE set. Runs in BOTH
    # cursor and page modes so the response shape stays uniform; cursor
    # consumers usually want a "Showing N of M" header anyway. Cheap at
    # current scale (signals table is small); revisit if it grows past
    # ~1M rows or if cursor latency becomes hot.
    total_stmt = _apply_filters(select(func.count()).select_from(Signal))
    total = (await session.execute(total_stmt)).scalar_one()

    if page is not None:
        # Offset path — used by the numbered pager UI.
        offset = (page - 1) * limit
        stmt = stmt.offset(offset).limit(limit)
        rows = (await session.execute(stmt)).all()
        page_rows = rows
        has_more = (offset + len(rows)) < total
        current_page: int | None = page
    else:
        # Cursor path — backwards-compatible default. `+1` row probes
        # has-more cheaply.
        stmt = stmt.limit(limit + 1)
        if cursor is not None:
            parsed = parse_cursor(cursor)
            if parsed is None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="invalid cursor",
                )
            cursor_ts, cursor_id = parsed
            # Composite cursor — direction flipped by sort.
            # newest: (created_at, id) < cursor → older rows come next
            # oldest: (created_at, id) > cursor → newer rows come next
            # Explicit OR form (not `tuple_`) because SQLAlchemy's row-value
            # comparison complains about mixing raw Python values with column
            # expressions at the type level.
            if asc_order:
                stmt = stmt.where(
                    or_(
                        Signal.created_at > cursor_ts,
                        and_(Signal.created_at == cursor_ts, Signal.id > cursor_id),
                    )
                )
            else:
                stmt = stmt.where(
                    or_(
                        Signal.created_at < cursor_ts,
                        and_(Signal.created_at == cursor_ts, Signal.id < cursor_id),
                    )
                )
        rows = (await session.execute(stmt)).all()
        has_more = len(rows) > limit
        page_rows = rows[:limit]
        # None — cursor traversal has no page number to honestly report.
        current_page = None

    items = [
        SignalListItem.from_row(row[0], outcome_id=row[1], alerted_to=row[2], now=now)
        for row in page_rows
    ]

    next_cursor: str | None = None
    if has_more and items:
        last = items[-1]
        next_cursor = format_cursor(last.created_at, last.id)

    # Ceiling division — total_pages = ceil(total / limit). Empty result =
    # 0 pages (not 1) so the pager renders nothing rather than "Page 1 of 1
    # · 0 results".
    total_pages = (total + limit - 1) // limit if total > 0 else 0

    return PaginatedSignals(
        items=items,
        next_cursor=next_cursor,
        total=total,
        page=current_page,
        total_pages=total_pages,
    )


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
