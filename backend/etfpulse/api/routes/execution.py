"""Execution API routes (PR D.4.3).

HTTP surface in front of the D.3 orchestrators. Every route is
authed via `get_current_user` (wallet-bound required) — the D.4.2
`/wallet/me` route is the only authed surface that admits a
wallet-less user.

Route map:

  POST /api/execution/prepare              build typed-data + INSERT Order
  POST /api/execution/submit/{id}          forward signature to gateway
  POST /api/execution/prepare-cancel/{id}  build cancel typed-data
  POST /api/execution/submit-cancel/{id}   forward cancel signature
  POST /api/execution/close-position/{id}  market-close an open position (P1.4)
  GET  /api/execution/orders               list user's orders (paginated)
  GET  /api/execution/orders/{id}          single order detail
  GET  /api/execution/positions            open positions
  GET  /api/execution/symbols              available SoDEX symbols for FE

Resource wiring:

  SoDEX HTTP clients are long-lived and owned by the scheduler
  lifespan (D.3.3) — stored on `app.state.sodex_{spot,perps}_client`.
  Submit routes pull them from there (NOT created per-request).
  If the bot is disabled / scheduler didn't boot, `app.state` lacks
  the attribute → 503 with operator-actionable hint.

Status mapping:

  - Risk DENY                     → 403 with `reason` + `detail`
  - SymbolNotResolved             → 503 with operator hint
  - Wrong-user order              → 404 (defense-in-depth; auth
                                     should catch it first)
  - Wrong-state transitions       → 409 (e.g., cancel of SUBMITTED)
  - Gateway 5xx                   → 502 propagated as 502; the
                                     orchestrator already records
                                     the in-DB state, so the FE can
                                     re-fetch the order to see the
                                     final state.

D14 contract: route owns the transaction boundary. We `await
session.commit()` exactly once per route at the success exit; on
exception the FastAPI dep generator + the DB context handle
rollback (asyncpg connection returns to the pool with the failed
tx discarded).
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import cast

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from etfpulse.adapters.sodex._http import SodexError, SodexHttpError
from etfpulse.adapters.sodex.perps_client import SodexPerpsClient
from etfpulse.adapters.sodex.schemas import (
    OrderSide as SodexOrderSide,
)
from etfpulse.adapters.sodex.schemas import (
    OrderType as SodexOrderType,
)
from etfpulse.adapters.sodex.schemas import (
    PositionSide as SodexPositionSide,
)
from etfpulse.adapters.sodex.schemas import (
    TimeInForce as SodexTimeInForce,
)
from etfpulse.adapters.sodex.spot_client import SodexSpotClient
from etfpulse.api.auth import get_current_user
from etfpulse.api.deps import get_db_session, get_sodex_clients
from etfpulse.api.schemas.execution import (
    VALID_VENUES,
    AccountSummaryResponse,
    BalanceOut,
    ExecutionLimitsResponse,
    FeeOut,
    MarkPriceOut,
    OrderOut,
    PaginatedOrders,
    PositionOut,
    PositionsResponse,
    PrepareCancelResponse,
    PrepareNewRequest,
    PrepareNewResponse,
    SubmitRequest,
    SubmitResponse,
    SymbolOut,
    SymbolsResponse,
    api_order_type_to_sodex,
    api_position_side_to_sodex,
    api_side_to_sodex,
    api_tif_to_sodex,
    api_trigger_type_to_sodex,
)
from etfpulse.config import settings
from etfpulse.models.order import TERMINAL_ORDER_STATUSES, Order, OrderStatus, Venue
from etfpulse.models.position import Position, PositionSide, PositionStatus
from etfpulse.models.sodex_symbol import SodexSymbol
from etfpulse.models.user import User
from etfpulse.pipeline.execution.pipeline import (
    PrepareResult,
    SubmitResult,
    prepare_cancel,
    prepare_new,
    submit_cancel,
    submit_new,
)
from etfpulse.pipeline.execution.risk import RiskRequest, compute_usage
from etfpulse.pipeline.execution.symbols import SymbolNotResolved
from etfpulse.pipeline.prices import Asset as PriceAsset
from etfpulse.pipeline.prices import get_spot_price_with_source
from etfpulse.pipeline.symbols_refresh import extract_asset_from_symbol_name

log = structlog.get_logger()
router = APIRouter(prefix="/execution", tags=["execution"])


# ---------------------------------------------------------------------------
# prepare / submit (new orders)
# ---------------------------------------------------------------------------


@router.post("/prepare", response_model=PrepareNewResponse)
async def post_prepare(
    body: PrepareNewRequest,
    session: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_user),
) -> PrepareNewResponse:
    """Run risk + build EIP-712 typed-data for a new order.

    Risk gate returns 403 on DENY with the orchestrator's reason in
    `detail`. The body deliberately carries the structured reason +
    breaker_trigger so the FE can render different UX for "halted"
    vs "cap exceeded" vs "venue unsupported".

    Symbol cache lookup may raise `SymbolNotResolved` if the venue's
    `/markets/symbols` hasn't been ingested yet (cold start). We
    surface that as 503 with an operator hint — the symbols-refresh
    cron / admin route is the resolution path.
    """
    if body.venue not in VALID_VENUES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"venue must be one of {sorted(VALID_VENUES)}",
        )

    risk_req = _to_risk_request(body)

    # Market orders carry no price on the wire (SoDEX market payload omits it),
    # but the risk gate needs a reference to size notional for the cap checks.
    # Resolve a spot reference the same way close-position does and thread it
    # onto the gate request. The builder serialises `price` for LIMIT only, so
    # this reference is used purely for risk sizing + the DB record and never
    # reaches the signed payload.
    #
    # ALWAYS override for market orders — we must NOT trust a client-supplied
    # `requested_price` on a market order: since it never reaches the wire, a
    # hostile client could understate it to size notional below the caps while
    # the venue fills at true market. The oracle price is the only trustworthy
    # reference, so it wins unconditionally for market orders.
    if risk_req.order_type == SodexOrderType.MARKET.value:
        asset_up = body.asset.upper()
        if asset_up not in {"BTC", "ETH"}:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"price oracle does not support asset={asset_up}",
            )
        price_result = await get_spot_price_with_source(cast(PriceAsset, asset_up))
        if price_result is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    f"spot price unavailable for asset={asset_up} — cannot size "
                    "market order. Retry shortly."
                ),
            )
        ref_price, _source = price_result
        risk_req = replace(risk_req, requested_price=ref_price)

    try:
        result: PrepareResult = await prepare_new(
            session,
            user_id=user.id,
            request=risk_req,
            signal_id=body.signal_id,
        )
    except SymbolNotResolved as exc:
        # Symbols cache miss — surface for operator action. Log the
        # uppercased asset so grep matches what hit the resolver (and
        # what's stored in `sodex_symbols.asset`); we forward
        # `body.asset.upper()` via the RiskRequest mapping below.
        normalized_asset = body.asset.upper()
        log.warning(
            "execution_prepare_symbol_not_resolved",
            venue=body.venue,
            asset=normalized_asset,
            user_id=user.id,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                f"symbol not resolved for venue={body.venue} asset={normalized_asset}. "
                "Admin can refresh via POST /api/admin/sodex/symbols/refresh."
            ),
        ) from exc

    if not result.allow:
        # Risk DENY. 403 with structured detail.
        log.info(
            "execution_prepare_denied",
            user_id=user.id,
            reason=result.reason,
            breaker_trigger=result.breaker_trigger,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "reason": result.reason,
                "detail": result.detail,
                "breaker_trigger": result.breaker_trigger,
            },
        )

    await session.commit()
    # `result.typed_data` (et al) are non-None on allow=True for new
    # orders per the orchestrator contract. Assert at runtime to
    # satisfy type checkers + surface a programmer-bug loudly. Note:
    # `python -O` strips asserts — in that mode, a None would still
    # land at Pydantic ResponseValidationError on the non-Optional
    # fields below, surfacing the same end state (500 via the
    # registered exception handler).
    assert result.typed_data is not None
    assert result.order_id is not None
    assert result.client_order_id is not None
    assert result.nonce is not None
    return PrepareNewResponse(
        order_id=result.order_id,
        client_order_id=result.client_order_id,
        nonce=result.nonce,
        typed_data=result.typed_data,
    )


@router.post("/submit/{order_id}", response_model=SubmitResponse)
async def post_submit(
    order_id: int,
    body: SubmitRequest,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_user),
) -> SubmitResponse:
    """Forward a wallet-signed order to the SoDEX gateway."""
    # Defense: confirm the Order belongs to this user. 404 (not 403)
    # so we don't leak the existence of someone else's order_id.
    await _ensure_user_owns_order(session, order_id=order_id, user_id=user.id)

    spot_client, perps_client = get_sodex_clients(request)

    result: SubmitResult = await submit_new(
        session,
        order_id=order_id,
        signature=body.signature,
        spot_client=spot_client,
        perps_client=perps_client,
    )
    await session.commit()
    return _submit_result_to_response(result)


# ---------------------------------------------------------------------------
# prepare-cancel / submit-cancel
# ---------------------------------------------------------------------------


@router.post("/prepare-cancel/{order_id}", response_model=PrepareCancelResponse)
async def post_prepare_cancel(
    order_id: int,
    session: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_user),
) -> PrepareCancelResponse:
    """Build cancel typed-data OR local-cancel a PENDING order.

    The orchestrator's status-branched logic (PENDING → local; SUBMITTED
    → DENY race-window; ACKED/PARTIALLY_FILLED → build typed-data;
    terminal → idempotent no-op) maps to:

      - allow=True + typed_data → 200 with typed_data
      - allow=True + local_only → 200 with local_only=True
      - allow=True + replayed   → 200 with replayed=True
      - allow=False (cancel_blocked_in_flight, unauthorized, etc) →
        409 with reason
    """
    await _ensure_user_owns_order(session, order_id=order_id, user_id=user.id)

    result: PrepareResult = await prepare_cancel(session, user_id=user.id, order_id=order_id)
    if not result.allow:
        log.info(
            "execution_prepare_cancel_denied",
            user_id=user.id,
            order_id=order_id,
            reason=result.reason,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"reason": result.reason, "detail": result.detail},
        )

    await session.commit()
    return PrepareCancelResponse(
        order_id=order_id,
        typed_data=result.typed_data,
        client_order_id=result.client_order_id,
        nonce=result.nonce,
        local_only=result.local_only,
        replayed=result.replayed,
    )


@router.post("/submit-cancel/{order_id}", response_model=SubmitResponse)
async def post_submit_cancel(
    order_id: int,
    body: SubmitRequest,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_user),
) -> SubmitResponse:
    """Forward a wallet-signed cancel to the gateway."""
    await _ensure_user_owns_order(session, order_id=order_id, user_id=user.id)
    spot_client, perps_client = get_sodex_clients(request)

    result: SubmitResult = await submit_cancel(
        session,
        order_id=order_id,
        signature=body.signature,
        spot_client=spot_client,
        perps_client=perps_client,
    )
    await session.commit()
    return _submit_result_to_response(result)


# ---------------------------------------------------------------------------
# close-position (PR P1.4)
# ---------------------------------------------------------------------------


@router.post("/close-position/{position_id}", response_model=PrepareNewResponse)
async def post_close_position(
    position_id: int,
    session: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_user),
) -> PrepareNewResponse:
    """Build a typed-data MARKET order that closes an open position.

    Same response shape as `/prepare`: the wallet still has to sign +
    submit. This endpoint is a convenience that resolves the position's
    asset/venue/size + opposite-side automatically; the user does NOT
    pick a side or size.

    - **Spot LONG** → SELL of the held quantity. `reduce_only=False`
      (spot rejects reduce_only per the risk gate; the SELL itself
      brings size to zero in the position-reconciler).
    - **Perps LONG** → SELL MARKET, `reduce_only=True`.
    - **Perps SHORT** → BUY MARKET, `reduce_only=True`.

    A 404 hides the existence of someone else's position_id. A 422
    surfaces if the position is already closed (`status != OPEN`) so the
    UI doesn't pump a no-op. Spot price fetch failure → 503 (same shape
    as `SymbolNotResolved` — operator can act).
    """
    # PR P1-fix.F1 — take FOR UPDATE on the User row at the top so the
    # dedupe SELECT below is atomic with the prepare_new INSERT that
    # follows. `check_order` (inside prepare_new) also acquires this
    # lock; in PostgreSQL the same session re-selecting FOR UPDATE on
    # an already-locked row is a no-op. Without this lock, two
    # concurrent close-position POSTs would both pass dedupe before
    # either committed and both insert close orders.
    locked_user = (
        await session.execute(select(User).where(User.id == user.id).with_for_update())
    ).scalar_one_or_none()
    if locked_user is None:
        # Race with /admin/users/{id}/unbind-wallet or similar — the
        # JWT was valid at auth-time, but the row vanished. 401 is the
        # honest answer.
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)

    position = await session.get(Position, position_id)
    if (
        position is None
        or position.user_id != user.id
        or position.status != PositionStatus.OPEN.value
    ):
        # Either not found, not yours, or already closed. Don't leak
        # which one — same 404 envelope as `_ensure_user_owns_order`.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    if position.size <= 0:
        # Defensive: an OPEN position with zero size is a corrupt row;
        # the reconciler should have flipped it to CLOSED. Surface as
        # 422 so the UI shows "nothing to close" without 5xx noise.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="position has zero size",
        )

    # PR P1-fix.B2 — short-circuit if a previous PERPS *immediate close*
    # for this (user, venue, asset) is still in-flight.
    #
    # The marker for "immediate close intent" is `reduce_only=True AND
    # is_conditional=False`. The `is_conditional=False` filter is
    # load-bearing (PR P1-fix.Issue-A): resting SL/TP legs are ALSO
    # `reduce_only=True` but carry `is_conditional=True` — without this
    # filter the dedupe would treat a resting stop as "a close already
    # in flight" and 409 every manual close on a protected position.
    #
    # On SPOT a close is a plain SELL indistinguishable from a regular
    # trade, so we cannot safely dedupe at this layer (the venue rejects
    # over-sell via insufficient-balance and the resulting REJECTED row
    # is the audit trail). 409 with the extant order_id so the FE can
    # surface "close already pending — wait or cancel" instead of
    # silently creating a second perps close.
    #
    # PR P1 review P1 fix — the dedupe is WINDOWED to recent closes only
    # (`execution_close_dedupe_window_seconds`, default 120s). The original
    # cut matched ANY non-terminal close, which trapped the user: a close
    # that was prepared but never signed + submitted (an abandoned
    # PENDING) blocked EVERY future close for the full 24h nonce window.
    # An unsigned/unsubmitted close can never reach the venue, so it can't
    # cause a double-execution — only a RECENT in-flight close (a genuine
    # double-click / double-POST) is worth blocking on. Outside the window
    # the abandoned order is ignored here and terminalised by the
    # nonce-expiry reaper. Window=0 disables the dedupe entirely.
    window_s = settings.execution_close_dedupe_window_seconds
    if position.venue == Venue.SODEX_PERPS.value and window_s > 0:
        cutoff = datetime.now(UTC) - timedelta(seconds=window_s)
        inflight_stmt = (
            select(Order.id)
            .where(Order.user_id == user.id)
            .where(Order.venue == position.venue)
            .where(Order.asset == position.asset)
            .where(Order.reduce_only.is_(True))
            .where(Order.is_conditional.is_(False))
            .where(Order.status.notin_([s.value for s in TERMINAL_ORDER_STATUSES]))
            .where(Order.created_at >= cutoff)
            .limit(1)
        )
        existing_close = (await session.execute(inflight_stmt)).scalar_one_or_none()
        if existing_close is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "reason": "close_already_in_flight",
                    "detail": (
                        f"a close order ({existing_close}) was just issued for "
                        f"{position.asset} on {position.venue}; wait for it to "
                        "settle, or cancel it from the orders list, before "
                        "issuing another close."
                    ),
                    "order_id": existing_close,
                },
            )

    # Resolve closing side from the position's directional side.
    if position.side == PositionSide.LONG.value:
        sodex_side = SodexOrderSide.SELL.value
    elif position.side == PositionSide.SHORT.value:
        sodex_side = SodexOrderSide.BUY.value
    else:  # pragma: no cover — DB CHECK pins side to {long, short}
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"unrecognised position side {position.side!r}",
        )

    is_perps = position.venue == Venue.SODEX_PERPS.value
    # `position.asset` is a str column; the price oracle only supports
    # BTC + ETH today. Narrow at runtime + 503 otherwise so a future
    # asset addition surfaces operator-actionable rather than 500.
    if position.asset not in {"BTC", "ETH"}:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"price oracle does not support asset={position.asset}",
        )
    # `position.asset` was just narrowed to {"BTC", "ETH"}; cast to the
    # matching Literal alias so mypy can verify the oracle call.
    price_result = await get_spot_price_with_source(cast(PriceAsset, position.asset))
    if price_result is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                f"spot price unavailable for asset={position.asset} — cannot "
                "size risk gate for close. Retry shortly."
            ),
        )
    ref_price, _source = price_result

    risk_req = RiskRequest(
        venue=position.venue,
        asset=position.asset,
        side=sodex_side,
        order_type=SodexOrderType.MARKET.value,
        time_in_force=SodexTimeInForce.IOC.value,
        requested_size=position.size,
        requested_price=ref_price,
        position_side=SodexPositionSide.BOTH.value if is_perps else None,
        # A close is an IMMEDIATE market reduce-only order, NOT a
        # conditional/stop order — so it carries no trigger_type and no
        # stop_price. (PR P1-fix.CRIT-1: trigger_type is now meaningful
        # in the signed payload, so we must not set it spuriously here.)
        trigger_type=None,
        # A close is reduce-only — it adds no exposure, so leverage is
        # immaterial to risk. But the perps risk gate still requires a
        # POSITIVE leverage value (validity check, not a cap). Positions
        # opened before leverage was mandatory carry NULL leverage; without
        # this fallback such a position could never be closed
        # (`perps_leverage_missing` DENY). Fall back to 1× so any held
        # position is always closable.
        leverage=(position.leverage or Decimal("1")) if is_perps else None,
        is_conditional=False,
        reduce_only=is_perps,
    )

    try:
        result: PrepareResult = await prepare_new(
            session,
            user_id=user.id,
            request=risk_req,
            signal_id=None,
        )
    except SymbolNotResolved as exc:
        log.warning(
            "execution_close_symbol_not_resolved",
            venue=position.venue,
            asset=position.asset,
            user_id=user.id,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                f"symbol not resolved for venue={position.venue} asset={position.asset}. "
                "Admin can refresh via POST /api/admin/sodex/symbols/refresh."
            ),
        ) from exc

    if not result.allow:
        log.info(
            "execution_close_position_denied",
            user_id=user.id,
            position_id=position_id,
            reason=result.reason,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "reason": result.reason,
                "detail": result.detail,
                "breaker_trigger": result.breaker_trigger,
            },
        )

    await session.commit()
    assert result.typed_data is not None
    assert result.order_id is not None
    assert result.client_order_id is not None
    assert result.nonce is not None
    return PrepareNewResponse(
        order_id=result.order_id,
        client_order_id=result.client_order_id,
        nonce=result.nonce,
        typed_data=result.typed_data,
    )


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


@router.get("/orders", response_model=PaginatedOrders)
async def list_orders(
    session: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_user),
    venue: str | None = Query(default=None),
    order_status: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> PaginatedOrders:
    """Paginated list of the user's orders, newest first.

    `status` filter accepts a single value (comma-list deferred — FE
    today renders separate tabs per filter). Unknown enum values are
    400ed via the OrderStatus check.
    """
    base = select(Order).where(Order.user_id == user.id)
    if venue is not None:
        if venue not in VALID_VENUES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"unknown venue {venue!r}",
            )
        base = base.where(Order.venue == venue)
    if order_status is not None:
        if order_status not in {s.value for s in OrderStatus}:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"unknown status {order_status!r}",
            )
        base = base.where(Order.status == order_status)

    # Total count for pagination. Same WHERE applied as the items
    # query so the count is exact for the filtered view.
    count_stmt = select(func.count()).select_from(base.subquery())
    total = (await session.execute(count_stmt)).scalar_one()

    items_stmt = base.order_by(Order.created_at.desc()).limit(limit).offset(offset)
    rows = (await session.execute(items_stmt)).scalars().all()
    return PaginatedOrders(
        items=[OrderOut.model_validate(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/orders/{order_id}", response_model=OrderOut)
async def get_order(
    order_id: int,
    session: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_user),
) -> OrderOut:
    """Single Order detail. 404 if not owned by the user (no existence leak)."""
    order = await session.get(Order, order_id)
    if order is None or order.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return OrderOut.model_validate(order)


@router.get("/positions", response_model=PositionsResponse)
async def list_positions(
    session: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_user),
) -> PositionsResponse:
    """Open positions for the user. Closed/cancelled positions are
    excluded — historical PnL surface belongs on a separate route."""
    stmt = (
        select(Position)
        .where(Position.user_id == user.id, Position.status == PositionStatus.OPEN.value)
        .order_by(Position.opened_at.desc())
    )
    rows = (await session.execute(stmt)).scalars().all()
    return PositionsResponse(items=[PositionOut.model_validate(r) for r in rows])


@router.get("/symbols", response_model=SymbolsResponse)
async def list_symbols(
    session: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_user),
    venue: str | None = Query(default=None),
) -> SymbolsResponse:
    """List SoDEX symbols cached on this server.

    Public per-user (just needs an authed session) since symbol
    metadata isn't sensitive. The FE uses this to render the
    venue/asset dropdown on the order form.

    Empty cache → empty list (not 503). The FE shows "no symbols
    available — admin must refresh" when items is empty.
    """
    stmt = select(SodexSymbol)
    if venue is not None:
        if venue not in VALID_VENUES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"unknown venue {venue!r}",
            )
        stmt = stmt.where(SodexSymbol.venue == venue)
    stmt = stmt.order_by(SodexSymbol.venue, SodexSymbol.asset, SodexSymbol.name)
    rows = (await session.execute(stmt)).scalars().all()
    return SymbolsResponse(
        items=[
            SymbolOut(
                venue=r.venue,
                symbol_id=r.symbol_id,
                name=r.name,
                asset=r.asset,
            )
            for r in rows
        ]
    )


# ---------------------------------------------------------------------------
# Limits + usage (P0) — DB-only, no SoDEX calls
# ---------------------------------------------------------------------------


@router.get("/limits", response_model=ExecutionLimitsResponse)
async def get_limits(
    session: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_user),
    asset: str | None = Query(default=None, max_length=10),
) -> ExecutionLimitsResponse:
    """Risk caps + the user's current usage against them.

    Lets the FE show limits + headroom (and warn pre-submit) instead of
    surfacing them only via a 403 risk-DENY. DB-only — reuses the gate's
    own `compute_usage` so the numbers match enforcement exactly.

    `asset` (canonical base, e.g. ``BTC``) scopes the per-symbol figure;
    omit it for the daily + open-order numbers alone. Normalised to
    uppercase so ``btc`` and ``BTC`` agree with the stored asset.
    """
    normalized_asset = asset.upper() if asset else None
    usage = await compute_usage(session, user_id=user.id, asset=normalized_asset)
    return ExecutionLimitsResponse(
        max_open_orders=settings.execution_max_open_orders_per_user,
        open_orders_used=usage.open_orders,
        daily_notional_cap=settings.execution_daily_notional_usd_cap,
        daily_notional_used=usage.daily_notional,
        per_symbol_cap=settings.execution_per_symbol_notional_usd_cap,
        per_symbol_used=usage.per_symbol_notional,
        asset=normalized_asset,
        max_leverage=settings.execution_max_leverage,
    )


# ---------------------------------------------------------------------------
# Account summary (P1/P2) — SoDEX reads: balances + fee + mark prices
# ---------------------------------------------------------------------------
#
# Per-call failure handling mirrors `wallet.py:get_sodex_bootstrap`:
# SodexHttpError(404) → empty/None (no account / no data on the gateway);
# any other SodexError (rate-limit, 5xx, envelope, parse) propagates so the
# route returns 503 and the FE degrades to a "summary unavailable" notice.


async def _safe_spot_balances(client: SodexSpotClient, address: str) -> list[BalanceOut]:
    """Spot balances → `[BalanceOut]`; 404 → empty (no SoDEX account)."""
    try:
        result = await client.get_balances(address)
    except SodexHttpError as exc:
        if exc.status_code == 404:
            return []
        raise
    out: list[BalanceOut] = []
    for b in result.balances:
        total = Decimal(b.total)
        locked = Decimal(b.locked)
        out.append(
            BalanceOut(asset=b.asset, total=total, locked=locked, available=total - locked)
        )
    return out


async def _safe_fee(client: SodexSpotClient, address: str) -> FeeOut | None:
    """Maker/taker fee rates; 404 → None (no account / no tier yet)."""
    try:
        fee = await client.get_fee_rate(address)
    except SodexHttpError as exc:
        if exc.status_code == 404:
            return None
        raise
    return FeeOut(maker_rate=Decimal(fee.maker_fee_rate), taker_rate=Decimal(fee.taker_fee_rate))


async def _safe_mark_prices(client: SodexPerpsClient) -> list[MarkPriceOut]:
    """Perps mark prices (market data, no address); 404 → empty."""
    try:
        marks = await client.get_mark_prices()
    except SodexHttpError as exc:
        if exc.status_code == 404:
            return []
        raise
    out: list[MarkPriceOut] = []
    for m in marks:
        try:
            base = extract_asset_from_symbol_name(m.symbol)
        except ValueError:
            # Malformed venue symbol — skip rather than poison the batch.
            continue
        out.append(
            MarkPriceOut(
                symbol=m.symbol,
                asset=base,
                mark_price=Decimal(m.mark_price),
                funding_rate=Decimal(m.funding_rate),
                next_funding_time=m.next_funding_time,
            )
        )
    return out


@router.get("/account-summary", response_model=AccountSummaryResponse)
async def get_account_summary(
    user: User = Depends(get_current_user),
    clients: tuple[SodexSpotClient, SodexPerpsClient] = Depends(get_sodex_clients),
) -> AccountSummaryResponse:
    """Aggregated SoDEX read state for the Execute page: spot balances, fee
    tier, and perps mark prices (live uPnL + funding).

    Read-only; wallet must be bound (`get_current_user`). The three reads run
    in parallel; a 404 on any degrades that field to empty/None, while a real
    SoDEX failure (rate-limit/5xx) surfaces as 503 so the FE shows a
    "summary unavailable" notice rather than a half-empty card.

    NOTE: for paper-trade users these reflect the REAL wallet on SoDEX — paper
    orders are simulated in our DB and never touch this. The FE labels that.
    """
    spot, perps = clients
    address = user.wallet_address
    assert address is not None  # get_current_user gates on bound wallet
    try:
        spot_balances, fee, mark_prices = await asyncio.gather(
            _safe_spot_balances(spot, address),
            _safe_fee(spot, address),
            _safe_mark_prices(perps),
        )
    except SodexError as exc:
        log.warning(
            "account_summary_failed",
            user_id=user.id,
            wallet=address,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="account_summary_unavailable",
        ) from exc
    except Exception as exc:  # noqa: BLE001
        # Defense-in-depth: this aggregates THREE external SoDEX reads. A
        # non-SodexError escape (e.g. a response-shape ValidationError or a
        # Decimal parse on an unexpected value) must NOT 500 the page — the FE
        # treats 503 as "summary unavailable" and degrades gracefully. Log the
        # full traceback at error level so the root cause is debuggable without
        # crashing the request.
        log.error(
            "account_summary_unexpected_error",
            user_id=user.id,
            wallet=address,
            error=str(exc),
            error_type=type(exc).__name__,
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="account_summary_unavailable",
        ) from exc
    return AccountSummaryResponse(
        spot_balances=spot_balances,
        fee=fee,
        mark_prices=mark_prices,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_risk_request(body: PrepareNewRequest) -> RiskRequest:
    """Map the wire-format API request to the SoDEX-IntEnum RiskRequest."""
    return RiskRequest(
        venue=body.venue,
        asset=body.asset.upper(),
        side=api_side_to_sodex(body.side.value),
        order_type=api_order_type_to_sodex(body.order_type.value),
        time_in_force=api_tif_to_sodex(body.time_in_force.value),
        requested_size=body.requested_size,
        requested_price=body.requested_price,
        position_side=(
            api_position_side_to_sodex(body.position_side.value)
            if body.position_side is not None
            else None
        ),
        trigger_type=(
            api_trigger_type_to_sodex(body.trigger_type.value)
            if body.trigger_type is not None
            else None
        ),
        leverage=body.leverage,
        is_conditional=body.is_conditional,
        stop_price=body.stop_price,
        stop_type=body.stop_type.value if body.stop_type is not None else None,
        reduce_only=body.reduce_only,
        parent_order_id=body.parent_order_id,
    )


async def _ensure_user_owns_order(session: AsyncSession, *, order_id: int, user_id: int) -> None:
    """Defense-in-depth: 404 if the order doesn't belong to this user.

    Returns existence-leak-free 404 for "not found" AND "not yours" so
    a probing client can't enumerate order IDs. The orchestrator paths
    (`prepare_cancel`, `submit_*`) also do user_id checks; this is the
    earlier-rejection layer.
    """
    order = await session.get(Order, order_id)
    if order is None or order.user_id != user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)


# `_get_sodex_clients` moved to `api/deps.py:get_sodex_clients` (SDXB.1)
# so the bootstrap route + execution route share one lookup.


def _submit_result_to_response(result: SubmitResult) -> SubmitResponse:
    return SubmitResponse(
        order_id=result.order_id,
        status=result.status,
        exchange_order_id=result.exchange_order_id,
        error_message=result.error_message,
        replayed=result.replayed,
    )
