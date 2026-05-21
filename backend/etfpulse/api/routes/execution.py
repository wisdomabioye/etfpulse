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

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from etfpulse.adapters.sodex.perps_client import SodexPerpsClient
from etfpulse.adapters.sodex.spot_client import SodexSpotClient
from etfpulse.api.auth import get_current_user
from etfpulse.api.deps import get_db_session
from etfpulse.api.schemas.execution import (
    VALID_VENUES,
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
from etfpulse.models.order import Order, OrderStatus
from etfpulse.models.position import Position, PositionStatus
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
from etfpulse.pipeline.execution.risk import RiskRequest
from etfpulse.pipeline.execution.symbols import SymbolNotResolved

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

    spot_client, perps_client = _get_sodex_clients(request)

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
    spot_client, perps_client = _get_sodex_clients(request)

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


def _get_sodex_clients(request: Request) -> tuple[SodexSpotClient, SodexPerpsClient]:
    """Pull the long-lived clients off `app.state`.

    These are entered into the scheduler lifespan (D.3.3) AsyncExitStack
    at boot. If the scheduler is disabled or didn't boot, `app.state`
    lacks the attributes → 503 with an operator hint.
    """
    spot = getattr(request.app.state, "sodex_spot_client", None)
    perps = getattr(request.app.state, "sodex_perps_client", None)
    if spot is None or perps is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "SoDEX clients not initialised on app.state. The scheduler "
                "lifespan owns these; check that RUN_SCHEDULER=true and "
                "SODEX_* env is set."
            ),
        )
    return spot, perps


def _submit_result_to_response(result: SubmitResult) -> SubmitResponse:
    return SubmitResponse(
        order_id=result.order_id,
        status=result.status,
        exchange_order_id=result.exchange_order_id,
        error_message=result.error_message,
        replayed=result.replayed,
    )
