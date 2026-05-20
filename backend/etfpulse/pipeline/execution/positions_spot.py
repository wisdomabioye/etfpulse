"""Spot position lifecycle — fill → Position row.

Spot is "we invent the position" — the venue tracks balances, not
positions. Our `Position` rows are the operator-facing record of
"the user bought X BTC at avg Y; this is what's still held."

Semantics:

  - **BUY fill**: open or extend a Position. For a fresh asset, INSERT
    OPEN with size=fill_size, entry_price=fill_price. For an existing
    OPEN position on the same `(user, asset, venue)`, aggregate via
    weighted-average entry:
        new_size = old_size + fill_size
        new_entry = (old_entry * old_size + fill_price * fill_size) / new_size

  - **SELL fill**: reduce the existing OPEN position. Realised PnL is
    accumulated per SELL fill (option 1 of the position-tracking
    choices in the plan):
        realized_pnl += (fill_price - position.entry_price) * fill_size
        size -= fill_size
    On `size <= 0`, transition to CLOSED with `closed_at` + final
    `close_price = fill_price`. A SELL with no matching OPEN position
    is an orphan — log and skip (V1; reconcile will surface the drift).

  - **Spot side**: always LONG. Spot has no short concept (you sell
    what you hold). PositionSide.SHORT on a spot row is invalid.

  - **Paper-trade**: `Order.paper_trade` is COPIED to the Position. A
    paper buy creates a paper Position; a paper sell closes a paper
    Position. Reconciliation does NOT touch paper Positions (the
    venue has no record of them).

D14: no commits — caller wraps the transaction.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from etfpulse.models.order import Order, OrderSide
from etfpulse.models.position import Position, PositionSide, PositionStatus

log = structlog.get_logger(__name__)

# Numeric(18, 8) column precision for entry/close/realized_pnl prices.
# Explicit ROUND_HALF_UP avoids the Decimal default (banker's rounding /
# ROUND_HALF_EVEN), which over many partial fills drifts the weighted-
# average entry slightly. PR D.3.1.
_PRICE_QUANTUM = Decimal("0.00000001")


async def spot_apply_fill(
    session: AsyncSession,
    *,
    order: Order,
    fill_price: Decimal,
    fill_size: Decimal,
) -> Position | None:
    """Apply a spot fill to the Position table.

    Returns the touched Position row (newly-inserted, aggregated, or
    closed). Returns None when a SELL hits with no matching OPEN
    position (orphan — drift to be resolved by reconcile).

    PRECONDITION: caller has classified this as a spot fill
    (`order.venue == Venue.SODEX_SPOT.value`). Mixing venues here is a
    programmer error; we don't double-check.
    """
    if fill_size <= 0:
        raise ValueError(f"spot_apply_fill: fill_size must be positive, got {fill_size}")
    if fill_price <= 0:
        raise ValueError(f"spot_apply_fill: fill_price must be positive, got {fill_price}")

    # Find an existing OPEN spot position for (user, asset, venue).
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

    if order.side == OrderSide.BUY.value:
        return await _apply_spot_buy(
            session,
            order=order,
            fill_price=fill_price,
            fill_size=fill_size,
            existing=existing,
        )
    elif order.side == OrderSide.SELL.value:
        return await _apply_spot_sell(
            session,
            order=order,
            fill_price=fill_price,
            fill_size=fill_size,
            existing=existing,
        )
    else:
        raise ValueError(f"spot_apply_fill: unknown order side {order.side!r}")


async def _apply_spot_buy(
    session: AsyncSession,
    *,
    order: Order,
    fill_price: Decimal,
    fill_size: Decimal,
    existing: Position | None,
) -> Position:
    """BUY: open or extend the LONG spot position."""
    if existing is None:
        position = Position(
            user_id=order.user_id,
            signal_id=order.signal_id,
            order_id=order.id,
            venue=order.venue,
            asset=order.asset,
            side=PositionSide.LONG.value,
            size=fill_size,
            entry_price=fill_price,
            status=PositionStatus.OPEN.value,
            paper_trade=order.paper_trade,
        )
        session.add(position)
        await session.flush()
        log.info(
            "spot_position_opened",
            user_id=order.user_id,
            asset=order.asset,
            size=str(fill_size),
            entry_price=str(fill_price),
            paper=order.paper_trade,
        )
        return position

    # Aggregate via weighted-average entry. Both decimals; precision
    # held at Numeric(28, 18) for size and Numeric(18, 8) for price.
    new_size = existing.size + fill_size
    weighted_entry = (existing.entry_price * existing.size + fill_price * fill_size) / new_size
    existing.size = new_size
    # Explicit ROUND_HALF_UP — banker's rounding (the Decimal default)
    # would drift the avg-entry slightly over many partial buys. PR D.3.1.
    existing.entry_price = Decimal(str(weighted_entry)).quantize(
        _PRICE_QUANTUM, rounding=ROUND_HALF_UP
    )
    await session.flush()
    log.info(
        "spot_position_extended",
        position_id=existing.id,
        user_id=order.user_id,
        asset=order.asset,
        new_size=str(new_size),
        new_entry=str(existing.entry_price),
        paper=order.paper_trade,
    )
    return existing


async def _apply_spot_sell(
    session: AsyncSession,
    *,
    order: Order,
    fill_price: Decimal,
    fill_size: Decimal,
    existing: Position | None,
) -> Position | None:
    """SELL: reduce or close the LONG spot position."""
    if existing is None:
        # Orphan — selling without holding. Spot semantics make this
        # impossible at the venue (it would reject the order) but our
        # reconcile loop or a paper-trade misuse could conceivably
        # produce one. Log + skip, don't crash.
        log.warning(
            "spot_position_sell_orphan",
            user_id=order.user_id,
            asset=order.asset,
            fill_size=str(fill_size),
            order_id=order.id,
        )
        return None

    # Realised PnL per fill: (sell - entry) * matched_size. Accumulates
    # across multiple partial sells.
    #
    # PR D.3.1 P0 fix: previously used `fill_size` directly, which on
    # over-sell (fill_size > existing.size — reachable via reconcile
    # drift or paper-trade misuse, since the venue rejects over-sells
    # on real spot) would over-count PnL on the excess units the user
    # didn't hold AND drive `existing.size` negative. Mirror perps:
    # match against the held size, ignore excess (logged below).
    matched_size = min(fill_size, existing.size)
    pnl_delta = (fill_price - existing.entry_price) * matched_size
    existing.realized_pnl = (existing.realized_pnl or Decimal("0")) + pnl_delta
    existing.size -= matched_size

    excess = fill_size - matched_size
    if excess > 0:
        log.warning(
            "spot_position_sell_excess",
            position_id=existing.id,
            user_id=order.user_id,
            asset=order.asset,
            attempted_excess=str(excess),
            note=(
                "SELL exceeded held size; closing matched portion only. "
                "Real spot venue would have rejected the order — likely "
                "paper-trade misuse or reconcile drift."
            ),
        )

    if existing.size <= 0:
        existing.status = PositionStatus.CLOSED.value
        existing.close_price = fill_price
        existing.closed_at = datetime.now(UTC)
        log.info(
            "spot_position_closed",
            position_id=existing.id,
            user_id=order.user_id,
            asset=order.asset,
            realized_pnl=str(existing.realized_pnl),
            close_price=str(fill_price),
            paper=order.paper_trade,
        )
    else:
        log.info(
            "spot_position_reduced",
            position_id=existing.id,
            user_id=order.user_id,
            asset=order.asset,
            new_size=str(existing.size),
            realized_pnl_so_far=str(existing.realized_pnl),
            paper=order.paper_trade,
        )
    await session.flush()
    return existing


async def spot_reconcile_positions(
    session: AsyncSession,
    *,
    user_id: int,
    venue: str,
    venue_balances: dict[str, Decimal],
) -> dict[str, int]:
    """Reconcile our OPEN spot Positions against venue balances.

    `venue_balances` is `{asset: held_qty}` from the gateway (the
    caller — `reconcile.py` — extracts this from
    `SodexSpotClient.get_balances/state`). Spot reconciliation is
    inherently inferred (no /positions endpoint on spot): we compare
    what the venue says we hold against our OPEN rows and resolve
    drift.

    Drift modes handled:
      - Venue balance == 0 AND we have OPEN Position → CLOSED with
        `close_price=NULL` (external sell happened; we don't know
        at what price). `realized_pnl` is left as-is (per-fill
        accumulator only updates on SELL fills we process directly).
        Operator review surface: the CLOSED row carries `closed_at`
        + `order_id` of the opening order, so the position trail
        is still recoverable.
      - Venue balance > 0 AND no OPEN Position → log
        `spot_position_orphan_from_venue` (we can't recover entry
        price). Operator may want to insert manually. V1 doesn't
        auto-create.
      - Sizes mismatch within tolerance — log only, no mutation.
        Spot's `size` is base-asset units; a tiny mismatch (rounding)
        is acceptable. Tolerance: relative 0.01% (1 bp).

    Paper-trade positions are NEVER touched here — they don't exist
    on venue. Filter is implicit: we only query `paper_trade=False`
    positions.

    D14: no commits. Returns counts: `{closed: N, orphans: M, drift: K}`.
    """
    # Tolerance for size comparison: 1 basis point (0.01%) — spot
    # decimal rounding can leave dust like 1e-15 BTC. Tolerate.
    tolerance = Decimal("0.0001")
    summary = {"closed": 0, "orphans": 0, "drift": 0}

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
        venue_qty = venue_balances.get(pos.asset, Decimal("0"))

        if venue_qty <= 0:
            # External close.
            pos.status = PositionStatus.CLOSED.value
            pos.closed_at = datetime.now(UTC)
            # `close_price` deliberately NULL — we don't know it.
            log.warning(
                "spot_position_closed_via_reconcile",
                position_id=pos.id,
                user_id=user_id,
                asset=pos.asset,
                our_size=str(pos.size),
                venue_balance=str(venue_qty),
            )
            summary["closed"] += 1
            continue

        # Size drift check.
        if pos.size != 0:
            relative_diff = abs(venue_qty - pos.size) / pos.size
            if relative_diff > tolerance:
                log.warning(
                    "spot_position_size_drift",
                    position_id=pos.id,
                    user_id=user_id,
                    asset=pos.asset,
                    our_size=str(pos.size),
                    venue_balance=str(venue_qty),
                    relative_diff_pct=str(relative_diff * 100),
                )
                summary["drift"] += 1

    # Orphans: assets the venue holds that we don't track.
    for asset, qty in venue_balances.items():
        if qty <= 0:
            continue
        if asset in our_assets:
            continue
        log.warning(
            "spot_position_orphan_from_venue",
            user_id=user_id,
            asset=asset,
            venue_balance=str(qty),
        )
        summary["orphans"] += 1

    if summary["closed"] or summary["orphans"] or summary["drift"]:
        await session.flush()
    return summary
