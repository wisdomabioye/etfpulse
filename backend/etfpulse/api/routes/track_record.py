"""Public track-record API — paginated outcomes + same-filter summary.

Stage 8-P4. Reads from `signal_outcomes` rows produced by the
`outcome_eval` scheduler job (Stage 8-P3). No auth (Phase 1 scope per
open_issues #43, same as `/api/signals` and `/api/regime`).

Three deliberate parallels with `routes/signals.py`:
    1. Same query-param shape (asset, signal_type, confidence_min) and
       semantics — frontend filter components are reusable.
    2. Same pagination — cursor + page modes mutually exclusive, both
       populate `total / next_cursor / page / total_pages` so the client
       picks the mode it needs.
    3. Same `_apply_filters` generic-on-Select pattern so the row query
       and the summary aggregate share one WHERE-builder. Drift between
       the two would mean "summary says 12 hits, list shows 14" bugs.

Why summary mirrors filters (not global): the dashboard's headline tile
(P5 — `/api/dashboard/stats`) carries the GLOBAL number. When the user
opens `/track-record` and filters to `asset=BTC`, they want to see the
BTC-specific hit rate next to the BTC-specific list. Two different
consumers, two different endpoints — clean separation.
"""

from __future__ import annotations

from typing import Any, Literal, TypeVar

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from etfpulse.api.deps import get_db_session
from etfpulse.api.schemas.signals import (
    AssetLiteral,
    SignalTypeLiteral,
    format_cursor,
    parse_cursor,
)
from etfpulse.api.schemas.track_record import (
    PaginatedTrackRecord,
    TrackRecordItemOut,
    TrackRecordSummary,
)
from etfpulse.models import Signal, SignalOutcome
from etfpulse.pipeline.track_record import (
    HORIZON_LABELS,
    compute_hit_rate_pct,
    get_stats_by_confidence_floor_and_horizon,
)

# PR B (#60) — `horizon` query param surface. Mirrors
# `pipeline.track_record.HorizonLabel`. `legacy` lets operators inspect the
# pre-v2 grandfathered rows in isolation — necessary while the wipe-and-
# reevaluate cleanup (#61) is pending.
HorizonFilterLiteral = Literal["scalp", "swing", "position", "legacy"]
# Inclusive boundaries — mirror `horizon_label_for` so the filter query
# can't drift from the bucketing helper. Each label maps to a
# `(min_hours, max_hours_exclusive)` range; NULL → "legacy" via a separate
# `IS NULL` filter rather than a range. Keeps the SQL trivially auditable.
_HORIZON_RANGE_HOURS: dict[HorizonFilterLiteral, tuple[int, int | None]] = {
    "scalp": (0, 24),
    "swing": (24, 96),
    "position": (96, 24 * 365 * 10),  # 10y cap — sentinel "no upper bound" for SQL
}

# Generic so `_apply_filters` returns the same Select shape it accepts —
# keeps the row query and the aggregate query sharing one WHERE-builder.
_S = TypeVar("_S", bound=Select[Any])

log = structlog.get_logger()
router = APIRouter(prefix="/track-record", tags=["track-record"])


@router.get("", response_model=PaginatedTrackRecord)
async def get_track_record(
    asset: AssetLiteral | None = Query(default=None),
    signal_type: SignalTypeLiteral | None = Query(default=None),
    confidence_min: int | None = Query(default=None, ge=1, le=10),
    # Issue #32 — cross-version track-record filter. Slice the public hit
    # rate by the prompt version that produced each signal so a prompt
    # bump (v2 → v3) doesn't pollute the headline number. Accepts any
    # `v[0-9]+` string; FE typically passes the value currently active
    # on the backend (surfaced via `/api/admin/metrics` for operators).
    ai_prompt_version: str | None = Query(
        default=None,
        pattern=r"^v[0-9]+$",
        description="Restrict to outcomes whose source signal was built with this prompt version.",
    ),
    # PR B (#60) — restrict to one horizon bucket. None = include all
    # buckets (including grandfathered `legacy` rows). The bucketed
    # `summary.hit_rate_by_horizon` is ALWAYS computed across all buckets
    # — that filter doesn't apply to the summary because the per-bucket
    # numbers are the whole point of the UI's "see all four side-by-side"
    # widget. Only the paginated `items` + the flat aggregate counts
    # (`total_evaluated`, `targets_hit`, etc) respect the filter.
    horizon: HorizonFilterLiteral | None = Query(
        default=None,
        description=(
            "Restrict to one horizon bucket: scalp (<24h), swing (24-96h), "
            "position (≥96h), legacy (NULL window_hours — pre-v2 rubric rows)."
        ),
    ),
    cursor: str | None = Query(default=None),
    page: int | None = Query(default=None, ge=1),
    limit: int = Query(default=20, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
) -> PaginatedTrackRecord:
    """Return summary stats + a paginated slice of evaluated outcomes.

    Sort order is fixed — newest evaluated first (`evaluated_at DESC, id DESC`).
    A second sort axis isn't useful here: the track record is a chronological
    transparency document, not a feed users browse by alphabetical asset.

    Pagination modes (mutually exclusive — `page` wins if both supplied):
      - `cursor` → keyset over `(evaluated_at, id)`, returns `next_cursor`.
      - `page` (1-based) → offset, returns `page / total_pages`.
    `total` is populated in BOTH modes via a single COUNT(*) — same trade-off
    as `/api/signals`.
    """

    # WHERE-clause builder shared by row + summary queries. `evaluated_at
    # IS NOT NULL` is defensive — the orchestrator always sets it on insert,
    # but the column is nullable, so a future writer (e.g. a manual seed)
    # could leak NULLs into the public surface. Filter at SQL so the FE
    # never sees them.
    def _apply_filters(q: _S) -> _S:
        q = q.where(SignalOutcome.evaluated_at.is_not(None))
        if asset is not None:
            q = q.where(SignalOutcome.asset == asset)
        if signal_type is not None:
            q = q.where(SignalOutcome.signal_type == signal_type)
        if confidence_min is not None:
            q = q.where(SignalOutcome.confidence >= confidence_min)
        if ai_prompt_version is not None:
            # `ai_prompt_version` lives on Signal, not SignalOutcome, so
            # we JOIN. Outcome → Signal is 1:1 via signal_id, so this
            # doesn't multiply rows. Only joined when the filter is set
            # to keep the unfiltered query path single-table-fast.
            q = q.join(Signal, Signal.id == SignalOutcome.signal_id).where(
                Signal.ai_prompt_version == ai_prompt_version
            )
        # PR B (#60) — horizon bucket filter on `window_hours`. `legacy`
        # is the NULL-window grandfathered bucket; the others use
        # half-open ranges that mirror `horizon_label_for` exactly.
        if horizon is not None:
            if horizon == "legacy":
                q = q.where(SignalOutcome.window_hours.is_(None))
            else:
                lo, hi = _HORIZON_RANGE_HOURS[horizon]
                q = q.where(
                    SignalOutcome.window_hours.is_not(None),
                    SignalOutcome.window_hours >= lo,
                    SignalOutcome.window_hours < hi,
                )
        return q

    # ------------------------------------------------------------------
    # Summary aggregate — single FILTER query (mirrors `dashboard.py`).
    # Counts `targeted_count` separately because hit_rate_pct divides by
    # it, NOT by total_evaluated. See the schema docstring for why.
    # ------------------------------------------------------------------
    summary_stmt = _apply_filters(
        select(
            func.count().label("total_evaluated"),
            func.count().filter(SignalOutcome.hit_target.is_(True)).label("targets_hit"),
            func.count().filter(SignalOutcome.hit_stop.is_(True)).label("stops_hit"),
            func.count().filter(SignalOutcome.hit_target.is_not(None)).label("targeted_count"),
            func.avg(SignalOutcome.confidence)
            .filter(SignalOutcome.hit_target.is_(True))
            .label("avg_conf_hits"),
            func.avg(SignalOutcome.confidence)
            .filter(SignalOutcome.hit_target.is_(False))
            .label("avg_conf_misses"),
        ).select_from(SignalOutcome)
    )
    summary_row = (await session.execute(summary_stmt)).one()

    # Postgres AVG returns Decimal (or NULL) — cast for clean JSON. NULL
    # passes through to None. Same pattern as `dashboard.py`.
    avg_hits = summary_row.avg_conf_hits
    avg_misses = summary_row.avg_conf_misses
    targeted = summary_row.targeted_count
    hit_rate = compute_hit_rate_pct(summary_row.targets_hit, targeted)

    # PR B (#60) — bucketed hit rate. Computed via the same
    # `get_stats_by_confidence_floor_and_horizon` helper used by the bot
    # cohort-stat call (PR B.3), so the UI's bucketed widget and the
    # bot's per-signal cohort line agree by construction. floor=1
    # cumulates across all confidence levels — same convention as the
    # global tile on /api/dashboard/stats.
    #
    # The bucketed view is intentionally NOT filtered by `horizon` (or by
    # the other filters): the widget's whole point is to compare buckets
    # side-by-side. The filtered `hit_rate_pct` above and the paginated
    # `items` below still respect the filter for the "list view" UX.
    by_horizon_stat = await get_stats_by_confidence_floor_and_horizon(session)
    hit_rate_by_horizon: dict[str, float | None] = {
        label: compute_hit_rate_pct(
            by_horizon_stat.by_floor_and_horizon[(1, label)][1],  # cumulative hits
            by_horizon_stat.by_floor_and_horizon[(1, label)][0],  # cumulative targeted
        )
        for label in HORIZON_LABELS
    }

    summary = TrackRecordSummary(
        total_evaluated=summary_row.total_evaluated,
        targets_hit=summary_row.targets_hit,
        stops_hit=summary_row.stops_hit,
        targeted_count=targeted,
        hit_rate_pct=hit_rate,
        hit_rate_by_horizon=hit_rate_by_horizon,
        avg_confidence_hits=float(avg_hits) if avg_hits is not None else None,
        avg_confidence_misses=float(avg_misses) if avg_misses is not None else None,
    )

    # ------------------------------------------------------------------
    # Paginated list — cursor or page mode.
    # ------------------------------------------------------------------
    base_stmt = select(SignalOutcome).order_by(
        SignalOutcome.evaluated_at.desc(), SignalOutcome.id.desc()
    )
    base_stmt = _apply_filters(base_stmt)

    # Total over the same WHERE — populated in BOTH modes so the client
    # always has a "Showing N of M" header. Reuses summary.total_evaluated
    # to avoid an extra COUNT roundtrip.
    total = summary.total_evaluated

    if page is not None:
        # Offset path — used by the numbered pager UI.
        offset = (page - 1) * limit
        rows = (await session.execute(base_stmt.offset(offset).limit(limit))).scalars().all()
        page_rows = list(rows)
        has_more = (offset + len(page_rows)) < total
        current_page: int | None = page
    else:
        # Cursor path — keyset over (evaluated_at, id) DESC. `+1` row probes
        # has-more cheaply.
        stmt = base_stmt.limit(limit + 1)
        if cursor is not None:
            parsed = parse_cursor(cursor)
            if parsed is None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="invalid cursor",
                )
            cursor_ts, cursor_id = parsed
            # DESC sort → "next page" is older rows → `(evaluated_at, id) <
            # cursor`. Explicit OR (not `tuple_`) for the same SQLAlchemy-
            # row-value-comparison reasons documented in `routes/signals.py`.
            stmt = stmt.where(
                or_(
                    SignalOutcome.evaluated_at < cursor_ts,
                    and_(
                        SignalOutcome.evaluated_at == cursor_ts,
                        SignalOutcome.id < cursor_id,
                    ),
                )
            )
        rows = (await session.execute(stmt)).scalars().all()
        has_more = len(rows) > limit
        page_rows = list(rows[:limit])
        current_page = None  # cursor traversal has no honest page number

    items = [TrackRecordItemOut.model_validate(row) for row in page_rows]

    next_cursor: str | None = None
    if has_more and items:
        last = page_rows[-1]
        # Defensive — the WHERE clause filters NULL evaluated_at out, so
        # this branch can't fire with a None timestamp; the assert pins
        # the invariant for mypy + a future change of the WHERE.
        assert last.evaluated_at is not None  # noqa: S101 — guarded by .where above
        next_cursor = format_cursor(last.evaluated_at, last.id)

    # Ceiling division — total_pages = ceil(total / limit). Empty result
    # → 0 pages so the pager renders nothing rather than "Page 1 of 1 ·
    # 0 results".
    total_pages = (total + limit - 1) // limit if total > 0 else 0

    return PaginatedTrackRecord(
        summary=summary,
        items=items,
        next_cursor=next_cursor,
        total=total,
        page=current_page,
        total_pages=total_pages,
    )
