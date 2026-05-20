"""Perps position lifecycle — fill → Position row.

Perps positions are first-class on the venue (`/positions` endpoint).
Our `Position` rows mirror the venue's view: one OPEN row per
`(user, asset, venue)` at any given time. A LONG position closing
and a fresh SHORT position opening on the same asset is TWO sequential
Position rows (CLOSED + OPEN), NOT one row whose side flipped.

Semantics:

  - **First fill on (user, asset, venue=perps)**: opens a new Position.
    Side derives from the Order:
        Order.side=BUY  → Position.side=LONG  (we're long the contract)
        Order.side=SELL → Position.side=SHORT (we're short the contract)
    `leverage` copied from the Order request.

  - **Same-side additional fill**: aggregate via weighted-average entry
    (same arithmetic as spot extend).

  - **Opposite-side fill**: reduce the existing Position.
        LONG  reduced by SELL of fill_size: pnl_delta = (fill - entry) * fill_size
        SHORT reduced by BUY  of fill_size: pnl_delta = (entry - fill) * fill_size
    On size <= 0, CLOSE with `close_price=fill_price`, `realized_pnl`
    accumulated.

  - **Flip rejected for V1**: a fill larger than the existing position
    on the opposite side would normally close + open a fresh position
    in the opposite direction. V1 doesn't support this — we close at
    the matched portion and log the excess as `perps_position_flip_excess`.
    The Order's `reduceOnly` flag should prevent this entirely from
    the venue side; the log is a defensive belt for misconfigured
    orders.

  - **Liquidation detection** lives in reconcile, not here. Apply_fill
    only processes fills we initiated.

D14: no commits — caller wraps the transaction.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from etfpulse.models.order import Order, OrderSide
from etfpulse.models.position import Position, PositionSide, PositionStatus
from etfpulse.pipeline.symbols_refresh import extract_asset_from_symbol_name

log = structlog.get_logger(__name__)

# See positions_spot._PRICE_QUANTUM — same Numeric(18,8) column type,
# same rounding-mode rationale.
_PRICE_QUANTUM = Decimal("0.00000001")


async def perps_apply_fill(
    session: AsyncSession,
    *,
    order: Order,
    fill_price: Decimal,
    fill_size: Decimal,
) -> Position | None:
    """Apply a perps fill to the Position table.

    Returns the touched Position (new, extended, reduced, or closed).
    Returns None on the "flip excess" path (the excess fill above the
    existing position size — we log and don't open the opposite-side
    position in V1).

    PRECONDITION: caller has classified this as a perps fill
    (`order.venue == Venue.SODEX_PERPS.value`).
    """
    if fill_size <= 0:
        raise ValueError(f"perps_apply_fill: fill_size must be positive, got {fill_size}")
    if fill_price <= 0:
        raise ValueError(f"perps_apply_fill: fill_price must be positive, got {fill_price}")

    # Find existing OPEN perps position for (user, asset, venue).
    existing = (
        await session.execute(
            select(Position).where(
                Position.user_id == order.user_id,
                Position.venue == order.venue,
                Position.asset == order.asset,
                Position.status == PositionStatus.OPEN.value,
                Position.paper_trade == order.paper_trade,
            )
        )
    ).scalar_one_or_none()

    # Derive the fill's directional side.
    if order.side == OrderSide.BUY.value:
        fill_side = PositionSide.LONG.value
    elif order.side == OrderSide.SELL.value:
        fill_side = PositionSide.SHORT.value
    else:
        raise ValueError(f"perps_apply_fill: unknown order side {order.side!r}")

    if existing is None:
        return await _perps_open(
            session,
            order=order,
            fill_price=fill_price,
            fill_size=fill_size,
            side=fill_side,
        )

    if existing.side == fill_side:
        return await _perps_extend(
            session,
            order=order,
            existing=existing,
            fill_price=fill_price,
            fill_size=fill_size,
        )

    return await _perps_reduce_or_close(
        session,
        order=order,
        existing=existing,
        fill_price=fill_price,
        fill_size=fill_size,
    )


async def _perps_open(
    session: AsyncSession,
    *,
    order: Order,
    fill_price: Decimal,
    fill_size: Decimal,
    side: str,
) -> Position:
    position = Position(
        user_id=order.user_id,
        signal_id=order.signal_id,
        order_id=order.id,
        venue=order.venue,
        asset=order.asset,
        side=side,
        size=fill_size,
        entry_price=fill_price,
        status=PositionStatus.OPEN.value,
        paper_trade=order.paper_trade,
        # Leverage column on Position is perps-only. The Order doesn't
        # carry a "leverage at fill" — risk.py validated the request's
        # leverage against the cap, then the orchestrator passes it
        # through. For V1, we encode "perps positions get a sensible
        # leverage value or NULL"; the actual binding lives on the
        # request, not the Order row. D.4 will thread it through via
        # the prepare flow. Until then, leave NULL.
        leverage=None,
    )
    session.add(position)
    await session.flush()
    log.info(
        "perps_position_opened",
        user_id=order.user_id,
        asset=order.asset,
        side=side,
        size=str(fill_size),
        entry_price=str(fill_price),
        paper=order.paper_trade,
    )
    return position


async def _perps_extend(
    session: AsyncSession,
    *,
    order: Order,
    existing: Position,
    fill_price: Decimal,
    fill_size: Decimal,
) -> Position:
    """Same-side additional fill — weighted-average entry, accumulate size."""
    new_size = existing.size + fill_size
    weighted_entry = (existing.entry_price * existing.size + fill_price * fill_size) / new_size
    existing.size = new_size
    # Explicit ROUND_HALF_UP — see positions_spot for rationale.
    existing.entry_price = Decimal(str(weighted_entry)).quantize(
        _PRICE_QUANTUM, rounding=ROUND_HALF_UP
    )
    await session.flush()
    log.info(
        "perps_position_extended",
        position_id=existing.id,
        user_id=order.user_id,
        asset=order.asset,
        side=existing.side,
        new_size=str(new_size),
        new_entry=str(existing.entry_price),
        paper=order.paper_trade,
    )
    return existing


async def _perps_reduce_or_close(
    session: AsyncSession,
    *,
    order: Order,
    existing: Position,
    fill_price: Decimal,
    fill_size: Decimal,
) -> Position:
    """Opposite-side fill — reduce or close. V1 rejects flip excess."""
    # Realised PnL direction depends on the existing position's side.
    # LONG reduced by SELL: gain when price > entry.
    # SHORT reduced by BUY: gain when entry > price.
    if existing.side == PositionSide.LONG.value:
        pnl_per_unit = fill_price - existing.entry_price
    else:  # SHORT
        pnl_per_unit = existing.entry_price - fill_price

    matched_size = min(fill_size, existing.size)
    pnl_delta = pnl_per_unit * matched_size
    existing.realized_pnl = (existing.realized_pnl or Decimal("0")) + pnl_delta
    existing.size -= matched_size

    if existing.size <= 0:
        existing.status = PositionStatus.CLOSED.value
        existing.close_price = fill_price
        existing.closed_at = datetime.now(UTC)
        log.info(
            "perps_position_closed",
            position_id=existing.id,
            user_id=order.user_id,
            asset=order.asset,
            side=existing.side,
            realized_pnl=str(existing.realized_pnl),
            close_price=str(fill_price),
            paper=order.paper_trade,
        )
    else:
        log.info(
            "perps_position_reduced",
            position_id=existing.id,
            user_id=order.user_id,
            asset=order.asset,
            side=existing.side,
            new_size=str(existing.size),
            realized_pnl_so_far=str(existing.realized_pnl),
            paper=order.paper_trade,
        )

    # Flip excess: caller asked to reduce by more than we held. V1 logs
    # and discards the excess rather than opening an opposite-side
    # position. The Order's reduceOnly flag should prevent this from
    # the venue side; this branch is a defensive belt.
    if fill_size > matched_size:
        excess = fill_size - matched_size
        log.warning(
            "perps_position_flip_excess",
            position_id=existing.id,
            user_id=order.user_id,
            asset=order.asset,
            attempted_excess=str(excess),
            order_id=order.id,
        )

    await session.flush()
    return existing


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


def _normalise_venue_position(row: dict[str, Any]) -> tuple[str, str, Decimal] | None:
    """Best-effort parse of a venue /positions row.

    V.2 had no perps positions captured, so the wire shape is
    `list[dict]` in `OpenPositionsResponse`. We probe common field
    names per api.md §"perps positions" and skip rows that lack the
    minimum required (symbol/asset + side + size).

    Returns `(asset, side, size)` on success, None to skip the row.
    The caller logs skipped rows so the wire-shape ambiguity surfaces
    in deploy logs and we can harden the parser once a non-empty
    capture exists.

    Heuristics:
      - asset: try `asset`, `symbol`, `s`; if the value contains '_'
        (e.g. 'vBTC_vUSDC'), take the leading segment and strip
        leading 'v'.
      - side: try `side`, `positionSide`, `S`; map to LONG/SHORT by
        string match. Numeric IntEnum values are also handled (1=LONG,
        2=SHORT per schema.md).
      - size: try `size`, `quantity`, `q`. Parsed as Decimal-shaped
        string (gateway returns these as quoted strings per api.md).
    """
    raw_asset = row.get("asset") or row.get("symbol") or row.get("s")
    if not isinstance(raw_asset, str) or not raw_asset:
        return None
    # PR D.3.4 — share the parser with `symbols_refresh` (single source of
    # truth for "what does this venue ticker normalise to"). The shared
    # helper raises on malformed input; here we'd rather skip the row
    # (matches the rest of this function's lenient parse policy), so we
    # catch ValueError + return None.
    try:
        asset = extract_asset_from_symbol_name(raw_asset)
    except ValueError:
        return None

    raw_side = row.get("side") or row.get("positionSide") or row.get("S")
    side: str
    if isinstance(raw_side, str):
        s = raw_side.upper()
        if s in ("LONG", "1"):
            side = PositionSide.LONG.value
        elif s in ("SHORT", "2"):
            side = PositionSide.SHORT.value
        else:
            return None
    elif isinstance(raw_side, int):
        if raw_side == 1:
            side = PositionSide.LONG.value
        elif raw_side == 2:
            side = PositionSide.SHORT.value
        else:
            return None
    else:
        return None

    raw_size = row.get("size") or row.get("quantity") or row.get("q")
    if raw_size is None:
        return None
    try:
        size = Decimal(str(raw_size))
    except (ValueError, TypeError, ArithmeticError):
        return None

    return asset, side, size


async def perps_reconcile_positions(
    session: AsyncSession,
    *,
    user_id: int,
    venue: str,
    venue_positions: list[dict[str, Any]],
) -> dict[str, int]:
    """Reconcile our OPEN perps Positions against venue `/positions` data.

    `venue_positions` is the parsed `OpenPositionsResponse.positions`
    list from `SodexPerpsClient.get_positions(...)`. Each entry is a
    `dict` per V.2's untyped capture (no typed item model yet).

    Drift modes:
      - Venue has position, we don't track it → `perps_position_orphan_from_venue`
        log (we can't recover entry_price; V1 doesn't auto-create).
      - We track OPEN, venue has zero size → CLOSE with `close_price=NULL`
        + log `perps_position_closed_via_reconcile` (likely external
        close OR LIQUIDATION — distinguishable only by venue's
        liquidation event stream, which isn't in V1 scope).
      - Both present, size drift > tolerance → log
        `perps_position_size_drift`.
      - Both present, side mismatch (we say LONG, venue says SHORT)
        → log `perps_position_side_mismatch` (severe drift, operator
        intervention needed; V1 doesn't auto-flip).

    Paper-trade positions are NEVER touched (the venue has no record
    of them). Filter is implicit: `paper_trade=False`.

    Skipped rows (unparseable shape) are logged but counted toward
    `skipped` so the wire-shape ambiguity surfaces in dashboards.

    D14: no commits. Returns
    `{closed: N, orphans: M, drift: K, side_mismatch: J, skipped: S}`.
    """
    tolerance = Decimal("0.0001")
    summary = {
        "closed": 0,
        "orphans": 0,
        "drift": 0,
        "side_mismatch": 0,
        "skipped": 0,
    }

    # Build {asset: (side, size)} from the venue payload, skipping
    # unparseable rows.
    venue_by_asset: dict[str, tuple[str, Decimal]] = {}
    for raw in venue_positions:
        parsed = _normalise_venue_position(raw)
        if parsed is None:
            log.warning(
                "perps_position_unparseable_venue_row",
                user_id=user_id,
                row=raw,
            )
            summary["skipped"] += 1
            continue
        asset, side, size = parsed
        if size <= 0:
            # Venue position with zero/negative size is closed on the
            # venue side. Don't include in the active set.
            continue
        venue_by_asset[asset] = (side, size)

    open_positions = (
        (
            await session.execute(
                select(Position).where(
                    Position.user_id == user_id,
                    Position.venue == venue,
                    Position.status == PositionStatus.OPEN.value,
                    Position.paper_trade.is_(False),
                )
            )
        )
        .scalars()
        .all()
    )

    our_assets: set[str] = set()

    for pos in open_positions:
        our_assets.add(pos.asset)
        venue_entry = venue_by_asset.get(pos.asset)

        if venue_entry is None:
            # External close or liquidation.
            pos.status = PositionStatus.CLOSED.value
            pos.closed_at = datetime.now(UTC)
            log.warning(
                "perps_position_closed_via_reconcile",
                position_id=pos.id,
                user_id=user_id,
                asset=pos.asset,
                our_side=pos.side,
                our_size=str(pos.size),
                possible_cause="external_close_or_liquidation",
            )
            summary["closed"] += 1
            continue

        venue_side, venue_size = venue_entry

        if pos.side != venue_side:
            log.error(
                "perps_position_side_mismatch",
                position_id=pos.id,
                user_id=user_id,
                asset=pos.asset,
                our_side=pos.side,
                venue_side=venue_side,
            )
            summary["side_mismatch"] += 1
            continue

        if pos.size != 0:
            relative_diff = abs(venue_size - pos.size) / pos.size
            if relative_diff > tolerance:
                log.warning(
                    "perps_position_size_drift",
                    position_id=pos.id,
                    user_id=user_id,
                    asset=pos.asset,
                    side=pos.side,
                    our_size=str(pos.size),
                    venue_size=str(venue_size),
                    relative_diff_pct=str(relative_diff * 100),
                )
                summary["drift"] += 1

    # Orphans: venue has a position we don't track.
    for asset, (side, size) in venue_by_asset.items():
        if asset in our_assets:
            continue
        log.warning(
            "perps_position_orphan_from_venue",
            user_id=user_id,
            asset=asset,
            venue_side=side,
            venue_size=str(size),
        )
        summary["orphans"] += 1

    if summary["closed"]:
        await session.flush()
    return summary
