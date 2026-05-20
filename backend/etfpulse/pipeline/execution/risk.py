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
from etfpulse.models.regime import CircuitBreaker
from etfpulse.models.user import User

logger = structlog.get_logger(__name__)


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

    per_user_trigger = await _find_active_breaker_trigger(session, user_id=user.id)
    if per_user_trigger is not None:
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
    # Per-venue: spot has no leverage concept; perps requires a positive
    # leverage value within the cap. Check leverage BEFORE the notional
    # aggregates so a misconfigured request fails on the cheaper gate.
    if request.venue == Venue.SODEX_PERPS.value:
        if request.leverage is None or request.leverage <= 0:
            return (
                _deny(
                    reason="perps_leverage_missing",
                    detail="Perps orders require positive leverage",
                ),
                user,
            )
        if request.leverage > settings.execution_max_leverage:
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

    # Compute the notional that this prospective order would add. Use
    # the explicit price when known (limit orders); fall back to a
    # cheap conservative estimate for market orders (no spot ref here —
    # caller should pre-resolve via prices.get_spot_price_with_source
    # before calling check_order so this branch never hits with
    # `requested_price=None`). If it does hit, we DENY: notional
    # checks against an unknown ref price would silently let big
    # orders through.
    if request.requested_price is None:
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
    new_notional = request.requested_size * request.requested_price

    # 24h rolling window.
    now = datetime.now(UTC)
    window_start = now - timedelta(hours=24)

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
