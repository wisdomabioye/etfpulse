"""Risk controller — pre-trade gates + per-user SELECT FOR UPDATE.

This module is the **single concurrency primitive** for the execution
surface (anti-drift rule 30). `check_order` opens
`SELECT ... FOR UPDATE` on the User row at the start; that lock holds
through the rest of `prepare_new` (risk → nonce → builder → Order
INSERT) and releases on the outer transaction's commit. Without this
lock, two concurrent `prepare_order` calls for the same user could
both pass the open-order cap, both pull `MAX(Order.nonce)`, and both
INSERT — busting caps and producing colliding nonces.

`check_order` returns BOTH the decision AND the locked User row so the
orchestrator doesn't need to re-read it. Lock is released by the route's
commit/rollback boundary.

D14: no commits, no writes (excluding the row lock).

Gates (fail-fast, cheapest-first within each tier):

  1. **Identity** (cheap, user-row reads)
     - wallet_address bound
     - sodex_account_id cached
     - sodex_{venue}_api_key_name registered for the requested venue

  2. **Breakers** (indexed reads on circuit_breakers)
     - any unresolved global breaker (user_id IS NULL)
     - any unresolved per-user breaker

  3. **Pre-flight enum gates** (cheap)
     - reject TimeInForce.FOK (not supported on venue)
     - reject TriggerType.LAST_PRICE / INDEX_PRICE (not supported)
     - reject PositionSide.LONG / SHORT (hedge-mode not supported)

  4. **Caps** (aggregate reads on orders)
     - open-order count < execution_max_open_orders_per_user
     - 24h notional (in-flight + recent-fill) <
       execution_daily_notional_usd_cap
     - per-symbol 24h notional <
       execution_per_symbol_notional_usd_cap
     - leverage <= execution_max_leverage  (perps only)

Anti-drift rule 29: every cap comes from `settings.execution_*`. No
hardcoded thresholds here. Reviewers: a literal numeric in this file
that isn't loaded from `settings` is a bug.

Scale follow-up (CLAUDE.md): the cap aggregates are SQL GROUP BYs per
call. At ~1k active orders per user the COUNT/SUM is fine; at 100k+
it isn't. Fix path: materialised per-user counters on a dedicated
`user_execution_state` row updated transactionally with order writes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from etfpulse.adapters.sodex.schemas import (
    OrderSide as SodexOrderSide,
)
from etfpulse.adapters.sodex.schemas import (
    PositionSide,
    TimeInForce,
    TriggerType,
)
from etfpulse.config import settings
from etfpulse.models.order import (
    TERMINAL_ORDER_STATUSES,
    Order,
    OrderStatus,
    Venue,
)
from etfpulse.models.order import (
    OrderSide as DbOrderSide,
)
from etfpulse.models.regime import CircuitBreaker, CircuitBreakerTrigger
from etfpulse.models.user import User

logger = structlog.get_logger(__name__)

# Side-comparison map for parent-link gate. `Order.side` persists as the
# DB StrEnum value (e.g. `"buy"`); `RiskRequest.side` carries the SoDEX
# IntEnum value (e.g. `1`). Cross-type equality always fails, so the
# parent-vs-child opposite-side check needs an explicit map.
_SODEX_TO_DB_SIDE: dict[int, str] = {
    SodexOrderSide.BUY.value: DbOrderSide.BUY.value,
    SodexOrderSide.SELL.value: DbOrderSide.SELL.value,
}


# ---------------------------------------------------------------------------
# Request value type
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RiskRequest:
    """Input to `check_order`. Pure value type — no API coupling.

    The orchestrator (`pipeline.py`) builds this from the validated
    `api/schemas/execution.py` request model. Keeping `RiskRequest` in
    `risk.py` avoids a cross-layer dependency on `api/`; the schemas
    package imports nothing from `pipeline/`, and `pipeline/` imports
    nothing from `api/`.

    Fields use the SoDEX IntEnum integer values (D.1's `schemas.py`),
    NOT the StrEnum strings on `models/order.py`. The intent is "what
    the gateway will see"; conversion happens at the API boundary.
    """

    venue: str  # `Venue.SODEX_SPOT.value` or `Venue.SODEX_PERPS.value`
    asset: str  # `"BTC"` | `"ETH"` — base asset code
    side: int  # `OrderSide.BUY = 1` | `OrderSide.SELL = 2`
    order_type: int  # `OrderType.LIMIT = 1` | `OrderType.MARKET = 2`
    time_in_force: int  # `TimeInForce.GTC/FOK/IOC/GTX`
    requested_size: Decimal  # base-asset quantity
    requested_price: Decimal | None  # limit price; None for market orders
    # Perps-only fields. For spot, all four should be None / default.
    position_side: int | None = None  # `PositionSide.BOTH = 1` (only supported)
    trigger_type: int | None = None  # `TriggerType.MARK_PRICE = 2` (only supported)
    leverage: Decimal | None = None  # 1..settings.execution_max_leverage on perps
    is_conditional: bool = False  # stop/trigger order
    # PR P1 — stop-loss / take-profit + reduce-only fields. `stop_type`
    # carries the `StopType` StrEnum value (`'stop_loss'`/`'take_profit'`)
    # — DB string, NOT a SoDEX IntEnum (no wire-side mapping; stops
    # ride on top of the entry order's typed-data in V1). All four
    # default to None/False so existing callers don't break.
    stop_price: Decimal | None = None
    stop_type: str | None = None
    reduce_only: bool = False
    parent_order_id: int | None = None


# ---------------------------------------------------------------------------
# Decision value type
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RiskDecision:
    """Result of `check_order`. Frozen — once decided, immutable.

    `allow=True` means every gate passed; the caller proceeds with
    nonce generation + INSERT. `allow=False` means a gate denied;
    `reason` is a stable identifier (snake_case, suitable for log
    grep) and `detail` is a human-readable string.

    `breaker_trigger` is populated when a circuit breaker gated the
    decision — distinct from cap-driven denials. UIs can render
    "Trading halted: <breaker_trigger>" differently from "Cap exceeded:
    <reason>".
    """

    allow: bool
    reason: str | None = None
    detail: str | None = None
    breaker_trigger: str | None = None


# ---------------------------------------------------------------------------
# Internals — venue/asset/key mapping
# ---------------------------------------------------------------------------


# Spot supports MARKET orders only via funds-based execution (no
# price), LIMIT via quantity + price. Perps supports both. Risk-side
# we treat all of these as valid; the gateway will reject anything it
# doesn't accept. This map is the "venue → api_key_name attribute"
# lookup — explicit rather than dynamic `getattr(user, f"sodex_{x}_api_key_name")`
# so a misspelled venue raises KeyError instead of silently returning None.
_VENUE_TO_API_KEY_FIELD: dict[str, str] = {
    Venue.SODEX_SPOT.value: "sodex_spot_api_key_name",
    Venue.SODEX_PERPS.value: "sodex_perps_api_key_name",
}


# Pre-flight enum gates — fields the gateway rejects so we surface
# faster + cleaner than waiting for envelope code -1.
_UNSUPPORTED_TIME_IN_FORCE: frozenset[int] = frozenset({TimeInForce.FOK.value})
_UNSUPPORTED_TRIGGER_TYPES: frozenset[int] = frozenset(
    {TriggerType.LAST_PRICE.value, TriggerType.INDEX_PRICE.value}
)
_UNSUPPORTED_POSITION_SIDES: frozenset[int] = frozenset(
    {PositionSide.LONG.value, PositionSide.SHORT.value}
)


# Statuses that count toward "in-flight" for the open-order cap and the
# notional aggregates. A signal-fired but unsigned PENDING still counts
# (it has reserved a nonce); ACKED/PARTIALLY_FILLED count (live on
# venue); FILLED counts in the 24h notional aggregate as realised
# exposure; terminal states (REJECTED/EXPIRED/CANCELLED) don't count.
_NON_TERMINAL_ORDER_STATUSES: frozenset[str] = frozenset(
    s.value for s in OrderStatus if s not in TERMINAL_ORDER_STATUSES
)

# Statuses that count toward 24h NOTIONAL exposure: everything except
# REJECTED/EXPIRED/CANCELLED (which never landed economic exposure).
# FILLED is included — a filled buy from 2h ago still counts toward
# today's cap because the capital is at risk.
_NOTIONAL_COUNTED_STATUSES: frozenset[str] = frozenset(
    s.value
    for s in OrderStatus
    if s not in {OrderStatus.REJECTED, OrderStatus.EXPIRED, OrderStatus.CANCELLED}
)

# Rolling window for the notional caps. Single source of truth — both the
# risk gate (`check_order`) AND the read-only usage snapshot (`compute_usage`,
# surfaced via GET /api/execution/limits) measure over this exact window, so
# what the UI shows and what the gate enforces can never drift.
NOTIONAL_WINDOW = timedelta(hours=24)


@dataclass(frozen=True, slots=True)
class UsageSnapshot:
    """Point-in-time view of a user's exposure against the risk caps.

    `per_symbol_notional` is None when no asset was requested (the daily +
    open-order numbers are asset-independent). All notionals are gross,
    both-sides, leverage-excluded — the same basis the gate enforces.
    """

    open_orders: int
    daily_notional: Decimal
    per_symbol_notional: Decimal | None


async def compute_usage(
    session: AsyncSession,
    *,
    user_id: int,
    asset: str | None,
) -> UsageSnapshot:
    """Compute a user's current exposure against the caps over `NOTIONAL_WINDOW`.

    Read-only. Reuses the SAME `_count_open_orders` + `_sum_notional` helpers
    and window the gate uses, so the surfaced numbers match enforcement
    exactly. `asset` (canonical base, e.g. "BTC") scopes the per-symbol
    figure; pass None to skip it.
    """
    window_start = datetime.now(UTC) - NOTIONAL_WINDOW
    open_orders = await _count_open_orders(session, user_id=user_id)
    daily_notional = await _sum_notional(
        session, user_id=user_id, window_start=window_start, asset=None
    )
    per_symbol_notional = (
        await _sum_notional(session, user_id=user_id, window_start=window_start, asset=asset)
        if asset is not None
        else None
    )
    return UsageSnapshot(
        open_orders=open_orders,
        daily_notional=daily_notional,
        per_symbol_notional=per_symbol_notional,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def check_order(
    session: AsyncSession,
    *,
    user_id: int,
    request: RiskRequest,
) -> tuple[RiskDecision, User]:
    """Lock the User row, run every risk gate, return decision + locked user.

    On `allow=False`, the caller MUST NOT proceed with the INSERT — the
    decision is final. On `allow=True`, the caller (orchestrator) chains:
    nonce generation → builder → Order INSERT, all inside the same
    transaction that holds this lock. Lock releases at commit/rollback.

    Returns `(decision, user)` so the caller doesn't re-read the user
    row (which would lose the FOR UPDATE guarantee).

    Raises:
        ValueError if `user_id` doesn't exist (programmer error —
        user_id should come from a JWT/session that's already validated
        the row exists).
    """
    # Acquire the row lock. `with_for_update()` translates to
    # `SELECT ... FOR UPDATE`; the row stays locked until tx commit.
    locked = await session.execute(select(User).where(User.id == user_id).with_for_update())
    user = locked.scalar_one_or_none()
    if user is None:
        raise ValueError(f"check_order: user_id={user_id} does not exist")

    # Gate 1 — identity (cheap, user-row only).
    if not user.wallet_address:
        return _deny(reason="wallet_not_bound", detail="User has no wallet bound"), user

    if user.sodex_account_id is None:
        return (
            _deny(
                reason="sodex_account_not_cached",
                detail="User has no cached SoDEX account_id — wallet bind incomplete",
            ),
            user,
        )

    api_key_field = _VENUE_TO_API_KEY_FIELD.get(request.venue)
    if api_key_field is None:
        # Unrecognised venue — should be impossible if the API schema
        # validates upstream, but defensive against drift.
        return (
            _deny(
                reason="unsupported_venue",
                detail=f"Venue {request.venue!r} is not a known SoDEX venue",
            ),
            user,
        )
    if not getattr(user, api_key_field):
        return (
            _deny(
                reason="api_key_not_registered",
                detail=f"User has not registered an API key for {request.venue}",
            ),
            user,
        )

    # Gate 2 — circuit breakers (indexed reads).
    global_trigger = await _find_active_breaker_trigger(session, user_id=None)
    if global_trigger is not None:
        return (
            _deny(
                reason="global_breaker_active",
                detail=f"Global circuit breaker active: {global_trigger}",
                breaker_trigger=global_trigger,
            ),
            user,
        )

    # PR P1-fix.BREAKER-1 (+ BREAKER-2 refinement) — an AUTOMATED per-user
    # breaker (e.g. daily_loss_limit, macro_event) is a RISK-MANAGEMENT
    # trip: it stops the user taking on MORE exposure. A `reduce_only`
    # order can only CLOSE/REDUCE — never open — so blocking it traps the
    # user in the very position the breaker is worried about (a daily-loss
    # trip that forbids closing is exactly backwards). Risk-reducing
    # orders are therefore EXEMPT from automated per-user breakers.
    #
    # EXCEPTION: a MANUAL per-user halt is an admin INCIDENT-RESPONSE
    # action (POST /api/admin/execution/halt {user_id}) — e.g. suspected
    # manipulation. That is a TOTAL freeze of the user, same intent as the
    # global breaker; reduce_only is NOT exempt, or the halted user could
    # still close (locking in P&L) and partially defeat the halt.
    #
    # The GLOBAL breaker above is never exempt (operator emergency freeze;
    # close pricing may be unreliable mid-incident). Spot closes are
    # reduce_only=False (spot has no reduce-only) so the exemption only
    # covers the dangerous leveraged-perps case; a blocked spot close
    # during a per-user trip is benign (no liquidation risk on spot).
    per_user_trigger = await _find_active_breaker_trigger(session, user_id=user.id)
    if per_user_trigger is not None:
        is_manual_halt = per_user_trigger == CircuitBreakerTrigger.MANUAL.value
        reduce_only_exempt = request.reduce_only and not is_manual_halt
        if not reduce_only_exempt:
            return (
                _deny(
                    reason="per_user_breaker_active",
                    detail=f"User circuit breaker active: {per_user_trigger}",
                    breaker_trigger=per_user_trigger,
                ),
                user,
            )

    # Gate 3 — pre-flight enum gates (cheap, in-memory).
    if request.time_in_force in _UNSUPPORTED_TIME_IN_FORCE:
        return (
            _deny(
                reason="unsupported_time_in_force",
                detail=(
                    f"TimeInForce={request.time_in_force} is not supported on "
                    "SoDEX (per schema.md §TimeInForceEnum)"
                ),
            ),
            user,
        )
    if request.trigger_type is not None and request.trigger_type in _UNSUPPORTED_TRIGGER_TYPES:
        return (
            _deny(
                reason="unsupported_trigger_type",
                detail=(
                    f"TriggerType={request.trigger_type} is not supported on "
                    "SoDEX (only MARK_PRICE today)"
                ),
            ),
            user,
        )
    if request.position_side is not None and request.position_side in _UNSUPPORTED_POSITION_SIDES:
        return (
            _deny(
                reason="unsupported_position_side",
                detail=(
                    f"PositionSide={request.position_side} is not yet supported "
                    "on SoDEX (only BOTH today; hedge-mode reserved)"
                ),
            ),
            user,
        )

    # Gate 4 — caps (aggregate reads).
    #
    # PR P1-fix.CAP-EXEMPT — a `reduce_only` order can only CLOSE/REDUCE a
    # position, never open new exposure, so the EXPOSURE-LIMITING caps
    # (leverage cap, open-order count, 24h + per-symbol notional) MUST NOT
    # block it — otherwise a user at any cap is trapped in a position they
    # can't close (same trap class as BREAKER-1). VALIDITY checks (perps
    # requires positive leverage; non-conditional orders require a
    # reference price) still apply. Mirrors the per-user-breaker exemption.
    is_reduce_only = request.reduce_only
    is_conditional = request.is_conditional

    # Per-venue: spot has no leverage concept; perps requires a positive
    # leverage value within the cap. Check leverage BEFORE the notional
    # aggregates so a misconfigured request fails on the cheaper gate.
    if request.venue == Venue.SODEX_PERPS.value:
        if request.leverage is None or request.leverage <= 0:
            # Validity — applies to ALL perps orders, reduce_only included.
            return (
                _deny(
                    reason="perps_leverage_missing",
                    detail="Perps orders require positive leverage",
                ),
                user,
            )
        if not is_reduce_only and request.leverage > settings.execution_max_leverage:
            # Exposure cap — exempt for reduce_only. A position opened at
            # 10x must remain closable after the cap is lowered to 5x.
            return (
                _deny(
                    reason="leverage_above_cap",
                    detail=(
                        f"Leverage {request.leverage} exceeds cap {settings.execution_max_leverage}"
                    ),
                ),
                user,
            )
    elif request.leverage is not None:
        # Spot must not carry a leverage value — meaningless on the venue.
        return (
            _deny(
                reason="spot_leverage_not_allowed",
                detail="Spot orders must not specify leverage",
            ),
            user,
        )

    # Gate 4b — PR P1: stop-attachment + reduce_only sanity. V1 supports
    # SL/TP and reduce-only ONLY on perps. Spot stops would require a
    # separate watcher (P2 paper-trade engine extends to live spot).
    is_perps = request.venue == Venue.SODEX_PERPS.value
    if request.stop_price is not None and not is_perps:
        return (
            _deny(
                reason="stop_price_perps_only",
                detail="Stop-loss / take-profit attachments are perps-only in V1",
            ),
            user,
        )
    if request.reduce_only and not is_perps:
        return (
            _deny(
                reason="reduce_only_perps_only",
                detail="reduce_only is perps-only in V1",
            ),
            user,
        )
    # PR P1-fix.CRIT-1 — a stop order (stop_price set) MUST carry a
    # trigger_type, else the gateway can't know what price feed arms
    # the stop. Without this gate, a stop_price would be signed into
    # the payload with `triggerType` omitted → the gateway either
    # rejects it or (worse) treats it as an immediate order. The
    # inverse — trigger_type set without stop_price — is allowed (a
    # plain perps market order may legitimately omit both; the close
    # path sets neither).
    if request.stop_price is not None and request.trigger_type is None:
        return (
            _deny(
                reason="stop_requires_trigger_type",
                detail="A stop order (stop_price set) must also set trigger_type",
            ),
            user,
        )
    # Gate 4c — parent_order_id linkage. Child (SL/TP) MUST reference a
    # real parent that belongs to the same user, same asset, same venue.
    # Opposite-side is enforced too: a SL on a BUY entry must be a SELL.
    # `reduce_only=True` is REQUIRED when parent is set — V1 children are
    # always position-closing intents, not new exposure.
    if request.parent_order_id is not None:
        parent = await session.get(Order, request.parent_order_id)
        if parent is None or parent.user_id != user.id:
            return (
                _deny(
                    reason="parent_order_not_found",
                    detail=(
                        f"parent_order_id={request.parent_order_id} does not belong to this user"
                    ),
                ),
                user,
            )
        if parent.venue != request.venue or parent.asset != request.asset:
            return (
                _deny(
                    reason="parent_order_mismatch",
                    detail="parent_order_id venue/asset must match this order",
                ),
                user,
            )
        # PR P1-fix.D1 — deny attaching a child to a parent that's
        # already in a terminal-failure state. REJECTED/EXPIRED/
        # CANCELLED parents never produced a position; SL/TP children
        # would queue, consume cap slots + nonces, and never fire.
        # FILLED + non-terminal in-flight states (PENDING, SUBMITTED,
        # ACKED, PARTIALLY_FILLED) all admit attachment.
        if parent.status in {
            OrderStatus.REJECTED.value,
            OrderStatus.EXPIRED.value,
            OrderStatus.CANCELLED.value,
        }:
            return (
                _deny(
                    reason="parent_order_terminal",
                    detail=(
                        f"parent_order_id={request.parent_order_id} is in "
                        f"terminal state {parent.status!r}; cannot attach"
                    ),
                ),
                user,
            )
        if parent.side == _SODEX_TO_DB_SIDE.get(request.side):
            return (
                _deny(
                    reason="parent_order_same_side",
                    detail="SL/TP child MUST be opposite-side to parent entry",
                ),
                user,
            )
        if not request.reduce_only:
            return (
                _deny(
                    reason="parent_requires_reduce_only",
                    detail="Orders with parent_order_id must be reduce_only",
                ),
                user,
            )

    # Open-order cap — exposure/resource limit; reduce_only exempt (a
    # close must not be blocked because the user has too many open
    # orders). `-1` sentinel in the log marks "not evaluated".
    open_count = -1
    if not is_reduce_only:
        open_count = await _count_open_orders(session, user_id=user.id)
        if open_count >= settings.execution_max_open_orders_per_user:
            return (
                _deny(
                    reason="open_order_cap_exceeded",
                    detail=(
                        f"User has {open_count} non-terminal orders; cap is "
                        f"{settings.execution_max_open_orders_per_user}"
                    ),
                ),
                user,
            )

    # Reference price — required for any order that EXECUTES IMMEDIATELY.
    # A CONDITIONAL order (SL/TP) rests until its trigger and legitimately
    # carries no immediate price (it uses stop_price), so it's exempt —
    # without this exemption the FE chain's market SL/TP legs (which carry
    # requested_price=None) would be wrongly denied `missing_requested_price`.
    if request.requested_price is None and not is_conditional:
        return (
            _deny(
                reason="missing_requested_price",
                detail=(
                    "Risk gate requires a reference price (limit price or pre-"
                    "resolved spot). Market orders must have a spot reference "
                    "attached upstream."
                ),
            ),
            user,
        )

    # Notional caps — exposure limits; reduce_only exempt. Also skipped
    # when there's no price to size against (only happens for the
    # conditional reduce_only legs already exempt above). Sentinels in
    # the log mark "not evaluated".
    new_notional = Decimal("0")
    daily_notional_existing = Decimal("0")
    per_symbol_existing = Decimal("0")
    if not is_reduce_only and request.requested_price is not None:
        new_notional = request.requested_size * request.requested_price

        # 24h rolling window — shared with `compute_usage` (the /limits
        # readout) via NOTIONAL_WINDOW so the gate + UI can't drift.
        now = datetime.now(UTC)
        window_start = now - NOTIONAL_WINDOW

        daily_notional_existing = await _sum_notional(
            session,
            user_id=user.id,
            window_start=window_start,
            asset=None,
        )
        if daily_notional_existing + new_notional > settings.execution_daily_notional_usd_cap:
            return (
                _deny(
                    reason="daily_notional_cap_exceeded",
                    detail=(
                        f"Daily notional {daily_notional_existing + new_notional} "
                        f"would exceed cap {settings.execution_daily_notional_usd_cap}"
                    ),
                ),
                user,
            )

        per_symbol_existing = await _sum_notional(
            session,
            user_id=user.id,
            window_start=window_start,
            asset=request.asset,
        )
        if per_symbol_existing + new_notional > settings.execution_per_symbol_notional_usd_cap:
            return (
                _deny(
                    reason="per_symbol_notional_cap_exceeded",
                    detail=(
                        f"Per-symbol notional for {request.asset} "
                        f"{per_symbol_existing + new_notional} would exceed cap "
                        f"{settings.execution_per_symbol_notional_usd_cap}"
                    ),
                ),
                user,
            )

    # All gates passed.
    logger.debug(
        "risk_check_allow",
        user_id=user.id,
        venue=request.venue,
        asset=request.asset,
        reduce_only=is_reduce_only,
        new_notional=str(new_notional),
        daily_notional_existing=str(daily_notional_existing),
        per_symbol_existing=str(per_symbol_existing),
        open_count=open_count,
    )
    return RiskDecision(allow=True), user


# ---------------------------------------------------------------------------
# Internals — query helpers
# ---------------------------------------------------------------------------


async def _find_active_breaker_trigger(session: AsyncSession, *, user_id: int | None) -> str | None:
    """Return the first unresolved trigger_type for the given scope, or None.

    `user_id=None` queries GLOBAL breakers (rows with user_id IS NULL).
    `user_id=<int>` queries that user's breakers.

    Both paths are covered by the partial index
    `ix_circuit_breakers_user_unresolved` (PR D.3) on
    `(user_id, trigger_type) WHERE resolved_at IS NULL`.
    """
    stmt = select(CircuitBreaker.trigger_type).where(CircuitBreaker.resolved_at.is_(None))
    if user_id is None:
        stmt = stmt.where(CircuitBreaker.user_id.is_(None))
    else:
        stmt = stmt.where(CircuitBreaker.user_id == user_id)
    stmt = stmt.limit(1)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def _count_open_orders(session: AsyncSession, *, user_id: int) -> int:
    """Count this user's orders in non-terminal status.

    The list mirrors `OrderStatus - TERMINAL_ORDER_STATUSES`. Computed
    at module load so it's static — adding a new status to the enum
    requires re-importing.
    """
    stmt = (
        select(func.count(Order.id))
        .where(Order.user_id == user_id)
        .where(Order.status.in_(_NON_TERMINAL_ORDER_STATUSES))
    )
    return int((await session.execute(stmt)).scalar_one())


async def _sum_notional(
    session: AsyncSession,
    *,
    user_id: int,
    window_start: datetime,
    asset: str | None,
) -> Decimal:
    """Sum exposure-counted notional for this user over the window.

    `SUM(requested_size * COALESCE(filled_price, requested_price))`
    over `status NOT IN (rejected, expired, cancelled) AND created_at
    >= window_start`. Filled orders are priced by realised execution;
    in-flight orders by request intent. Excludes orders that never
    landed economic exposure (REJECTED/EXPIRED/CANCELLED).

    Note: market orders without `requested_price` will produce a NULL
    contribution and be excluded — but `check_order` already denies
    requests with `requested_price is None` upstream, so this branch
    only fires for legacy / paper-trade rows where the price columns
    landed differently. V1 is permissive on those; a future refinement
    can fold a venue spot-reference for market fills.

    `asset=None` returns user-wide sum; non-None scopes to one asset.

    Returns 0 if no matching rows (SQL SUM of empty = NULL → coerce).
    """
    # COALESCE order: realised price wins over request intent. Both
    # may be NULL on paper trades + market orders before spot resolves;
    # SUM ignores NULL contributions naturally, so the rare NULL row
    # is skipped (not counted as zero). The CANCELLED+partial-fill
    # edge case (filled_size > 0) is documented above.
    price_expr = func.coalesce(
        Order.filled_price,
        Order.requested_price,
    )
    stmt = (
        select(func.coalesce(func.sum(Order.requested_size * price_expr), 0))
        .where(Order.user_id == user_id)
        .where(Order.status.in_(_NOTIONAL_COUNTED_STATUSES))
        .where(Order.created_at >= window_start)
    )
    if asset is not None:
        stmt = stmt.where(Order.asset == asset)
    result = (await session.execute(stmt)).scalar_one()
    # asyncpg returns Decimal for NUMERIC; func.coalesce with int 0
    # may return Decimal or int depending on the path — coerce defensively.
    return Decimal(str(result)) if result is not None else Decimal("0")


def _deny(*, reason: str, detail: str, breaker_trigger: str | None = None) -> RiskDecision:
    """One-line denial constructor — keeps the call sites compact."""
    return RiskDecision(allow=False, reason=reason, detail=detail, breaker_trigger=breaker_trigger)


# Re-export the request side of the contract so callers
# (`pipeline.py`, tests) don't have to import from a deep path.
__all__ = [
    "RiskDecision",
    "RiskRequest",
    "check_order",
]
