"""Order + position reconciliation loop.

Runs every `order_reconcile_interval_seconds` (default 60s) as an
APScheduler `IntervalTrigger` job. Resolves drift between our local
Order/Position state and the SoDEX gateway's view via per-user
`GET /account/orders/open` + `GET /balances` (spot) /
`GET /positions` (perps).

Scope (V1):
  - **Fill detection**: venue's `filledQuantity` > our `filled_size`
    → fold the delta as a fill (calls `positions_{spot,perps}.apply_fill`).
    On full fill: status → FILLED.
  - **Position reconcile inline**: after the order loop, call
    `spot_reconcile_positions` and `perps_reconcile_positions` with
    the same data we already fetched (no extra HTTP).
  - **Orphan logging**: an Order locally non-terminal but missing on
    venue, past `order_reconcile_orphan_grace_seconds`, gets logged
    (`reconcile_drift_unmatched`). Status flip is left to the nonce-
    expiry reaper — the conservative correct terminalisation.

NOT in scope for V1 (deferred):
  - External cancel detection (the venue cancelled, we still ACKED).
    Operator would manually clear; nonce expiry catches it eventually.
  - Order-status mapping beyond fill-detection (status names vary by
    venue without a non-empty capture to pin them).

Per-user error containment: one user's HTTP failure does NOT kill the
sweep. Wrapped in try/except, error counted in summary, log + continue.

D14: does NOT commit. The scheduler wrapper commits per tick.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from etfpulse.adapters.sodex.perps_client import SodexPerpsClient
from etfpulse.adapters.sodex.spot_client import SodexSpotClient
from etfpulse.config import settings
from etfpulse.models.order import (
    TERMINAL_ORDER_STATUSES,
    Order,
    OrderStatus,
    Venue,
)
from etfpulse.models.user import User
from etfpulse.pipeline.execution.positions_perps import (
    perps_apply_fill,
    perps_reconcile_positions,
)
from etfpulse.pipeline.execution.positions_spot import (
    spot_apply_fill,
    spot_reconcile_positions,
)

log = structlog.get_logger(__name__)


# Statuses reconcile considers "active" (worth probing the venue).
# Excludes terminals (no value in checking; they're settled).
_RECONCILABLE_STATUSES: frozenset[str] = frozenset(
    s.value for s in OrderStatus if s not in TERMINAL_ORDER_STATUSES
)


async def reconcile_open_orders(
    session: AsyncSession,
    *,
    spot_client: SodexSpotClient,
    perps_client: SodexPerpsClient,
) -> dict[str, int]:
    """Reconcile every user with non-terminal orders against the venue.

    Returns a summary count dict. Per-user errors are counted in
    `user_errors` and logged but do NOT abort the sweep.
    """
    summary = {
        "users_processed": 0,
        "orders_filled": 0,
        "orders_partially_filled": 0,
        "orders_drift_unmatched": 0,
        "user_errors": 0,
        # Position-reconcile counters (folded in from spot/perps modules):
        "positions_closed_via_reconcile": 0,
        "positions_orphans": 0,
        "positions_drift": 0,
        "positions_side_mismatch": 0,
        "positions_skipped_unparseable": 0,
    }

    # Min-age cutoff so freshly-submitted orders aren't reconciled too
    # eagerly (their ACK may still be in flight).
    #
    # PR D.3.1 — filter on `Order.created_at`, not `updated_at`. The
    # original D.3 used `updated_at`, which advances on EVERY status
    # change (server_onupdate=now()) — an order that churned PENDING→
    # SUBMITTED→ACKED in 30s gets excluded even if it's been minutes
    # since `created_at`. `created_at` cleanly captures "order is at
    # least N seconds old since it was first created."
    min_age_cutoff = datetime.now(UTC) - timedelta(seconds=settings.order_reconcile_min_age_seconds)

    # Distinct user_ids with at least one reconcilable order past
    # min_age. We exclude paper orders explicitly — paper has no venue
    # state to reconcile.
    user_ids_q = (
        select(Order.user_id)
        .where(Order.user_id.is_not(None))
        .where(Order.status.in_(_RECONCILABLE_STATUSES))
        .where(Order.paper_trade.is_(False))
        .where(Order.created_at < min_age_cutoff)
        .distinct()
    )
    user_ids = (await session.execute(user_ids_q)).scalars().all()

    for user_id in user_ids:
        if user_id is None:
            # Orders.user_id can be NULL after a user is deleted
            # (ondelete=SET NULL); skip — nothing to reconcile.
            continue
        try:
            await _reconcile_one_user(
                session,
                user_id=user_id,
                spot_client=spot_client,
                perps_client=perps_client,
                summary=summary,
            )
            summary["users_processed"] += 1
        except Exception as exc:  # noqa: BLE001
            # Per-user containment: one user's HTTP failure / venue
            # parse-error / unexpected exception must NOT kill the
            # sweep. PR D.3.1 documentation: the broad `Exception`
            # IS intentional — it catches both known recoverable
            # types (SodexError, asyncpg.PostgresError, asyncio.
            # TimeoutError) AND programmer errors. The `log.exception`
            # preserves the full traceback so programmer errors are
            # still observable; the `user_errors` counter reports
            # the breakage in operator dashboards. Narrowing to a
            # specific union would let an unexpected TypeError silently
            # kill the sweep for ALL subsequent users in the loop —
            # the resilience tradeoff is the documented choice.
            summary["user_errors"] += 1
            log.exception(
                "reconcile_user_failed",
                user_id=user_id,
                error=str(exc),
            )

    log.info("reconcile_sweep_complete", **summary)
    return summary


# ---------------------------------------------------------------------------
# Per-user reconcile
# ---------------------------------------------------------------------------


async def _reconcile_one_user(
    session: AsyncSession,
    *,
    user_id: int,
    spot_client: SodexSpotClient,
    perps_client: SodexPerpsClient,
    summary: dict[str, int],
) -> None:
    """Reconcile orders + positions for one user, one tick.

    Loads:
      - User (for wallet_address — needed for GETs).
      - Non-terminal orders, grouped by venue.

    For each venue with active orders:
      1. GET /account/orders/open → match by clOrdID, fold fills.
      2. GET /balances or /positions → reconcile positions.

    Both calls happen even if step 1 found no drift — the position
    state is independent of the orders we know about (could be drift
    from external trades).
    """
    user = (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None or not user.wallet_address:
        # User got deleted or unbound mid-sweep — defensive skip.
        return

    # Group active orders by venue.
    orders = (
        (
            await session.execute(
                select(Order)
                .where(Order.user_id == user_id)
                .where(Order.status.in_(_RECONCILABLE_STATUSES))
                .where(Order.paper_trade.is_(False))
            )
        )
        .scalars()
        .all()
    )
    orders_by_venue: dict[str, list[Order]] = {}
    for o in orders:
        orders_by_venue.setdefault(o.venue, []).append(o)

    # Per-venue reconciliation. V1 scope: "only if active orders." A
    # future change that probes API-key presence (to catch externally-
    # held positions) would broaden these conditions — but adds N×venues
    # unnecessary HTTP calls per tick for every user with a wallet bound.
    # When that lands, replace the boolean below with a real check.
    # (D.3.2 cleaned up the always-False `_user_has_*` helpers that
    # used to live here; the conditions are flattened.)
    if Venue.SODEX_SPOT.value in orders_by_venue:
        await _reconcile_user_venue_spot(
            session,
            user=user,
            orders=orders_by_venue.get(Venue.SODEX_SPOT.value, []),
            spot_client=spot_client,
            summary=summary,
        )

    if Venue.SODEX_PERPS.value in orders_by_venue:
        await _reconcile_user_venue_perps(
            session,
            user=user,
            orders=orders_by_venue.get(Venue.SODEX_PERPS.value, []),
            perps_client=perps_client,
            summary=summary,
        )


# ---------------------------------------------------------------------------
# Spot venue
# ---------------------------------------------------------------------------


async def _reconcile_user_venue_spot(
    session: AsyncSession,
    *,
    user: User,
    orders: list[Order],
    spot_client: SodexSpotClient,
    summary: dict[str, int],
) -> None:
    """Reconcile one user's spot orders + positions."""
    address = user.wallet_address or ""

    # GET /account/orders/open — list[dict] of open orders.
    open_resp = await spot_client.get_open_orders(address)
    venue_open_by_clordid = _index_venue_orders_by_clordid(open_resp.orders)

    # Fold fills + detect orphans.
    for order in orders:
        await _process_one_order_fill(
            session,
            order=order,
            venue_open_by_clordid=venue_open_by_clordid,
            summary=summary,
        )

    # Position reconcile: fetch balances + diff.
    balances_resp = await spot_client.get_balances(address)
    venue_balances: dict[str, Decimal] = {}
    for entry in balances_resp.balances:
        try:
            qty = Decimal(entry.total)
        except (InvalidOperation, ValueError):
            continue
        # Asset from BalanceEntry.asset (already a clean symbol like 'vBTC').
        # Normalise via the same rule as `symbols_refresh`.
        from etfpulse.pipeline.symbols_refresh import extract_asset_from_symbol_name

        asset = extract_asset_from_symbol_name(entry.asset)
        venue_balances[asset] = qty

    pos_summary = await spot_reconcile_positions(
        session,
        user_id=user.id,
        venue=Venue.SODEX_SPOT.value,
        venue_balances=venue_balances,
    )
    summary["positions_closed_via_reconcile"] += pos_summary["closed"]
    summary["positions_orphans"] += pos_summary["orphans"]
    summary["positions_drift"] += pos_summary["drift"]


# ---------------------------------------------------------------------------
# Perps venue
# ---------------------------------------------------------------------------


async def _reconcile_user_venue_perps(
    session: AsyncSession,
    *,
    user: User,
    orders: list[Order],
    perps_client: SodexPerpsClient,
    summary: dict[str, int],
) -> None:
    address = user.wallet_address or ""

    open_resp = await perps_client.get_open_orders(address)
    venue_open_by_clordid = _index_venue_orders_by_clordid(open_resp.orders)

    for order in orders:
        await _process_one_order_fill(
            session,
            order=order,
            venue_open_by_clordid=venue_open_by_clordid,
            summary=summary,
        )

    positions_resp = await perps_client.get_positions(address)
    pos_summary = await perps_reconcile_positions(
        session,
        user_id=user.id,
        venue=Venue.SODEX_PERPS.value,
        venue_positions=positions_resp.positions,
    )
    summary["positions_closed_via_reconcile"] += pos_summary["closed"]
    summary["positions_orphans"] += pos_summary["orphans"]
    summary["positions_drift"] += pos_summary["drift"]
    summary["positions_side_mismatch"] += pos_summary["side_mismatch"]
    summary["positions_skipped_unparseable"] += pos_summary["skipped"]


# ---------------------------------------------------------------------------
# Order-fill folding
# ---------------------------------------------------------------------------


async def _process_one_order_fill(
    session: AsyncSession,
    *,
    order: Order,
    venue_open_by_clordid: dict[str, dict],
    summary: dict[str, int],
) -> None:
    """Match one local order against the venue's /open response.

    Three branches:
      1. Found on venue + filled_quantity > our filled_size →
         fold the DELTA via positions_{spot,perps}.apply_fill.
      2. Found on venue + filled_quantity == our filled_size →
         no-op (state in sync).
      3. NOT found on venue → orphan check. If past orphan grace +
         past nonce expiry → already terminal by reaper. If past
         orphan grace + still within nonce window → log only.
    """
    venue_row = venue_open_by_clordid.get(order.client_order_id)

    if venue_row is None:
        # Order not on venue's open list.
        await _handle_unmatched_order(order=order, summary=summary)
        return

    venue_filled = _safe_decimal(venue_row.get("filledQuantity"))
    venue_filled_funds = _safe_decimal(venue_row.get("filledFunds"))
    if venue_filled is None:
        # Defensive: shape we don't recognise. Skip.
        return

    our_filled = order.filled_size or Decimal("0")
    if venue_filled <= our_filled:
        # No new fill; state in sync.
        return

    delta_size = venue_filled - our_filled
    # Derive fill price: prefer venue's `avgPrice` if present, else
    # filled_funds / venue_filled (volume-weighted average).
    fill_price = _derive_fill_price(
        venue_row=venue_row,
        filled_quantity=venue_filled,
        filled_funds=venue_filled_funds,
        fallback=order.requested_price,
    )
    if fill_price is None:
        log.warning(
            "reconcile_fill_price_undetermined",
            order_id=order.id,
            client_order_id=order.client_order_id,
        )
        return

    # Persist the fill on the Order row before calling apply_fill.
    #
    # PR D.3.4 correction: `apply_fill` (spot + perps) does NOT read
    # `Order.filled_*` — it takes `fill_price` + `fill_size` as
    # explicit kwargs. The pre-set here is for AUDIT (the Order row
    # reflects the cumulative venue state after this reconcile tick);
    # the actual position math uses the delta passed in below.
    #
    # Audit-field caveat: `filled_value` is incremented per delta
    # × fill_price, while `filled_price` is OVERWRITTEN with the
    # most-recent derivation. Over multiple partial fills using the
    # `requested_price` fallback path, accumulated `filled_value` is
    # NOT guaranteed to equal `filled_size × filled_price`. Audit-only
    # today; if reporting downstream ever depends on the equality,
    # compute a true VWAP from the delta stream instead.
    order.filled_size = venue_filled
    order.filled_price = fill_price
    order.filled_value = (order.filled_value or Decimal("0")) + (delta_size * fill_price)

    # Status transition: requested_size satisfied → FILLED; else
    # PARTIALLY_FILLED.
    if venue_filled >= order.requested_size:
        order.status = OrderStatus.FILLED.value
        summary["orders_filled"] += 1
    else:
        order.status = OrderStatus.PARTIALLY_FILLED.value
        summary["orders_partially_filled"] += 1

    # Apply the delta to the Position table.
    if order.venue == Venue.SODEX_SPOT.value:
        await spot_apply_fill(
            session,
            order=order,
            fill_price=fill_price,
            fill_size=delta_size,
        )
    elif order.venue == Venue.SODEX_PERPS.value:
        await perps_apply_fill(
            session,
            order=order,
            fill_price=fill_price,
            fill_size=delta_size,
        )

    log.info(
        "reconcile_fill_folded",
        order_id=order.id,
        venue=order.venue,
        delta_size=str(delta_size),
        fill_price=str(fill_price),
        new_status=order.status,
    )


async def _handle_unmatched_order(*, order: Order, summary: dict[str, int]) -> None:
    """Order not on venue's open list. Log when past orphan grace;
    leave status to nonce-expiry reaper."""
    # PR D.3.1 — name reflects "cutoff timestamp", not "duration",
    # and we compare `updated_at` against it. updated_at is the
    # right field here (not created_at as in the main filter): a
    # SUBMITTED order that JUST transitioned should NOT be flagged
    # as orphan even if `created_at` is hours old.
    orphan_grace_cutoff_at = datetime.now(UTC) - timedelta(
        seconds=settings.order_reconcile_orphan_grace_seconds
    )
    if order.updated_at < orphan_grace_cutoff_at:
        summary["orders_drift_unmatched"] += 1
        log.warning(
            "reconcile_drift_unmatched",
            order_id=order.id,
            venue=order.venue,
            client_order_id=order.client_order_id,
            status=order.status,
            updated_at=order.updated_at.isoformat(),
            note="order missing on venue past orphan grace; nonce expiry will terminalise",
        )


# ---------------------------------------------------------------------------
# Defensive parsing helpers
# ---------------------------------------------------------------------------


def _index_venue_orders_by_clordid(rows: list[dict]) -> dict[str, dict]:
    """Build `{clOrdID: row}` from a list[dict] /orders/open response.
    Rows lacking clOrdID are skipped (defensive — V.2 had no captures
    to pin the shape, so rare missing fields shouldn't crash the sweep).

    PR D.3.1 — log the count of dropped rows. A real-world cause for
    a row lacking `clOrdID` is an order placed by the user directly
    via the SoDEX UI (not via ETFPulse). Today the executor's caps +
    awareness are blind to externally-placed orders; surfacing the
    count here gives operators visibility on the existence of such
    orders (next-stage work: track per-user external-order count
    explicitly).
    """
    out: dict[str, dict] = {}
    dropped = 0
    for row in rows:
        co = row.get("clOrdID")
        if isinstance(co, str) and co:
            out[co] = row
        else:
            dropped += 1
    if dropped > 0:
        log.info(
            "reconcile_venue_rows_without_clordid",
            count=dropped,
            note="likely externally-placed orders (SoDEX UI, not via ETFPulse)",
        )
    return out


def _safe_decimal(raw: Any) -> Decimal | None:
    """Decimal-shaped string → Decimal, or None on unparseable."""
    if raw is None:
        return None
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _derive_fill_price(
    *,
    venue_row: dict,
    filled_quantity: Decimal,
    filled_funds: Decimal | None,
    fallback: Decimal | None,
) -> Decimal | None:
    """Compute the average fill price.

    Preference order:
      1. `avgPrice` field on the venue row (if present + parseable).
      2. `filled_funds / filled_quantity` if both available.
      3. Fallback (typically the limit price — only valid for clean
         LIMIT fills with no slippage).

    Returns None if none of the above produces a positive Decimal.
    """
    raw_avg = venue_row.get("avgPrice") or venue_row.get("avg_price")
    avg = _safe_decimal(raw_avg)
    if avg is not None and avg > 0:
        return avg
    if filled_funds is not None and filled_quantity > 0:
        derived = filled_funds / filled_quantity
        if derived > 0:
            return derived
    if fallback is not None and fallback > 0:
        return fallback
    return None
