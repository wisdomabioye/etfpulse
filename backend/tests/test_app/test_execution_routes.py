"""PR D.4.3 — Execution API route tests.

Pins the HTTP boundary in front of D.3's orchestrators. Mock SoDEX
clients on `app.state` so submit paths don't reach the network. The
underlying orchestrator logic is tested exhaustively in
`tests/test_pipeline/test_execution_pipeline.py` — this file covers
the route layer only:

  - auth: 401/403 paths via the D.4.1 dep
  - mapping: API string enums → SoDEX IntEnums via RiskRequest
  - risk DENY → 403 with structured detail
  - SymbolNotResolved → 503
  - missing sodex clients on app.state → 503
  - cross-user defense: 404 on someone else's order_id
  - prepare-cancel state branches (PENDING local-only, SUBMITTED
    → 409, terminal replayed)
  - read endpoints: orders/list pagination + filters, single order,
    positions (open only), symbols list
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from etfpulse.adapters.sodex.perps_client import SodexPerpsClient
from etfpulse.adapters.sodex.responses import OrderResponseItem
from etfpulse.adapters.sodex.spot_client import SodexSpotClient
from etfpulse.api.auth import mint_jwt
from etfpulse.api.deps import get_db_session
from etfpulse.app import create_app
from etfpulse.models import Order, OrderStatus, Position, SodexSymbol, User, Venue
from etfpulse.models.position import PositionStatus


def _wallet() -> str:
    return "0x" + secrets.token_hex(20)


async def _seed_user(db_session, *, paper_trade: bool = True) -> User:
    """Paper-trade default True so the submit path can short-circuit
    without touching the gateway-mock."""
    u = User(
        wallet_address=_wallet(),
        sodex_account_id=57436,
        sodex_spot_api_key_name="default",
        sodex_perps_api_key_name="default",
        paper_trade=paper_trade,
    )
    db_session.add(u)
    await db_session.flush()
    return u


async def _seed_btc_spot_symbol(db_session) -> None:
    db_session.add(
        SodexSymbol(
            venue=Venue.SODEX_SPOT.value,
            symbol_id=1,
            name="vBTC_vUSDC",
            asset="BTC",
            raw={"id": 1, "name": "vBTC_vUSDC"},
            refreshed_at=datetime.now(UTC),
        )
    )
    await db_session.flush()


def _mock_clients() -> tuple[SodexSpotClient, SodexPerpsClient]:
    """In-memory mocks with a successful ACK envelope item."""
    ok_item = OrderResponseItem(code=0, orderID=12345, clOrdID="cli-xyz")
    spot = AsyncMock(spec=SodexSpotClient)
    spot.submit_batch_new_order = AsyncMock(return_value=[ok_item])
    spot.submit_batch_cancel_order = AsyncMock(return_value=[ok_item])
    perps = AsyncMock(spec=SodexPerpsClient)
    perps.submit_batch_new_order = AsyncMock(return_value=[ok_item])
    perps.submit_batch_cancel_order = AsyncMock(return_value=[ok_item])
    return spot, perps


@pytest.fixture
async def app_and_client(
    db_session: AsyncSession,
) -> AsyncIterator[tuple[FastAPI, httpx.AsyncClient]]:
    app = create_app()

    async def _override_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db_session] = _override_session
    # Attach mock SoDEX clients to app.state (scheduler does this in
    # production via the lifespan AsyncExitStack).
    spot, perps = _mock_clients()
    app.state.sodex_spot_client = spot
    app.state.sodex_perps_client = perps

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as c:
        yield app, c


def _auth(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {mint_jwt(user.id)}"}


def _spot_prepare_body(**overrides) -> dict:
    base = {
        "venue": Venue.SODEX_SPOT.value,
        "asset": "BTC",
        "side": "buy",
        "order_type": "limit",
        "time_in_force": "gtc",
        "requested_size": "0.01",
        "requested_price": "65000",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Auth gating
# ---------------------------------------------------------------------------


async def test_prepare_401_unauth(app_and_client):
    _, client = app_and_client
    r = await client.post("/api/execution/prepare", json=_spot_prepare_body())
    assert r.status_code == 401


async def test_prepare_403_wallet_unbound(app_and_client, db_session):
    _, client = app_and_client
    u = User(wallet_address=None)
    db_session.add(u)
    await db_session.flush()
    r = await client.post(
        "/api/execution/prepare",
        json=_spot_prepare_body(),
        headers={"Authorization": f"Bearer {mint_jwt(u.id)}"},
    )
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# prepare happy path
# ---------------------------------------------------------------------------


async def test_prepare_happy_path_spot(app_and_client, db_session):
    _, client = app_and_client
    await _seed_btc_spot_symbol(db_session)
    user = await _seed_user(db_session)
    r = await client.post("/api/execution/prepare", json=_spot_prepare_body(), headers=_auth(user))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["order_id"]
    assert body["client_order_id"].startswith("ep-s-")
    assert body["nonce"]
    assert body["typed_data"]["primaryType"] == "ExchangeAction"


async def test_prepare_503_on_missing_symbol(app_and_client, db_session):
    """No SodexSymbol row → SymbolNotResolved → 503 with operator hint."""
    _, client = app_and_client
    user = await _seed_user(db_session)
    r = await client.post(
        "/api/execution/prepare",
        json=_spot_prepare_body(),
        headers=_auth(user),
    )
    assert r.status_code == 503
    assert "symbol" in r.json()["detail"]


async def test_prepare_403_on_risk_deny_unsupported_tif(app_and_client, db_session):
    """FOK is in the wire-enum but rejected at risk gate (gateway
    doesn't support it). DENY → 403 with reason."""
    _, client = app_and_client
    await _seed_btc_spot_symbol(db_session)
    user = await _seed_user(db_session)
    r = await client.post(
        "/api/execution/prepare",
        json=_spot_prepare_body(time_in_force="fok"),
        headers=_auth(user),
    )
    assert r.status_code == 403, r.text
    body = r.json()
    assert body["detail"]["reason"] == "unsupported_time_in_force"


async def test_prepare_422_on_unknown_venue(app_and_client, db_session):
    _, client = app_and_client
    user = await _seed_user(db_session)
    r = await client.post(
        "/api/execution/prepare",
        json=_spot_prepare_body(venue="binance"),
        headers=_auth(user),
    )
    assert r.status_code == 422


async def test_prepare_422_on_invalid_side(app_and_client, db_session):
    _, client = app_and_client
    user = await _seed_user(db_session)
    r = await client.post(
        "/api/execution/prepare",
        json=_spot_prepare_body(side="INVALID"),
        headers=_auth(user),
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# submit happy path (paper trade short-circuit) + clients
# ---------------------------------------------------------------------------


async def test_submit_paper_trade_happy_path(app_and_client, db_session):
    _, client = app_and_client
    await _seed_btc_spot_symbol(db_session)
    user = await _seed_user(db_session, paper_trade=True)
    prep = await client.post(
        "/api/execution/prepare",
        json=_spot_prepare_body(),
        headers=_auth(user),
    )
    assert prep.status_code == 200
    order_id = prep.json()["order_id"]

    # Build a valid signature shape (paper-trade ignores the value, only
    # validates shape).
    sig = "0x01" + "a" * 130
    r = await client.post(
        f"/api/execution/submit/{order_id}",
        json={"signature": sig},
        headers=_auth(user),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["order_id"] == order_id
    assert body["status"] in {"filled", "acked"}


async def test_submit_422_on_bad_signature_shape(app_and_client, db_session):
    _, client = app_and_client
    await _seed_btc_spot_symbol(db_session)
    user = await _seed_user(db_session)
    prep = await client.post(
        "/api/execution/prepare",
        json=_spot_prepare_body(),
        headers=_auth(user),
    )
    order_id = prep.json()["order_id"]
    r = await client.post(
        f"/api/execution/submit/{order_id}",
        json={"signature": "0xabc"},
        headers=_auth(user),
    )
    assert r.status_code == 422


async def test_submit_404_on_other_users_order(app_and_client, db_session):
    _, client = app_and_client
    await _seed_btc_spot_symbol(db_session)
    user_a = await _seed_user(db_session)
    user_b = await _seed_user(db_session)
    prep = await client.post(
        "/api/execution/prepare",
        json=_spot_prepare_body(),
        headers=_auth(user_a),
    )
    order_id = prep.json()["order_id"]
    r = await client.post(
        f"/api/execution/submit/{order_id}",
        json={"signature": "0x01" + "a" * 130},
        headers=_auth(user_b),
    )
    assert r.status_code == 404


async def test_submit_503_when_clients_missing(db_session):
    """Lifespan didn't run → no clients on app.state → 503."""
    app = create_app()

    async def _override_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db_session] = _override_session
    # NOTE: don't attach sodex clients
    await _seed_btc_spot_symbol(db_session)
    user = await _seed_user(db_session)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as c:
        prep = await c.post(
            "/api/execution/prepare",
            json=_spot_prepare_body(),
            headers=_auth(user),
        )
        # prepare doesn't need clients; submit does.
        order_id = prep.json()["order_id"]
        r = await c.post(
            f"/api/execution/submit/{order_id}",
            json={"signature": "0x01" + "a" * 130},
            headers=_auth(user),
        )
    assert r.status_code == 503


# ---------------------------------------------------------------------------
# prepare-cancel / submit-cancel
# ---------------------------------------------------------------------------


async def test_prepare_cancel_pending_returns_local_only(app_and_client, db_session):
    _, client = app_and_client
    await _seed_btc_spot_symbol(db_session)
    user = await _seed_user(db_session)
    prep = await client.post(
        "/api/execution/prepare",
        json=_spot_prepare_body(),
        headers=_auth(user),
    )
    order_id = prep.json()["order_id"]
    # Order is PENDING — cancel locally.
    r = await client.post(
        f"/api/execution/prepare-cancel/{order_id}",
        headers=_auth(user),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["local_only"] is True
    assert body["typed_data"] is None


async def test_prepare_cancel_409_on_submitted_state(app_and_client, db_session):
    """SUBMITTED → orchestrator returns allow=False + cancel_blocked_in_flight
    → route returns 409."""
    _, client = app_and_client
    await _seed_btc_spot_symbol(db_session)
    user = await _seed_user(db_session)
    prep = await client.post(
        "/api/execution/prepare",
        json=_spot_prepare_body(),
        headers=_auth(user),
    )
    order_id = prep.json()["order_id"]
    # Manually flip to SUBMITTED so prepare_cancel hits the race-window branch.
    order = await db_session.get(Order, order_id)
    order.status = OrderStatus.SUBMITTED.value
    await db_session.flush()
    r = await client.post(
        f"/api/execution/prepare-cancel/{order_id}",
        headers=_auth(user),
    )
    assert r.status_code == 409


async def test_prepare_cancel_replayed_on_terminal(app_and_client, db_session):
    _, client = app_and_client
    await _seed_btc_spot_symbol(db_session)
    user = await _seed_user(db_session)
    prep = await client.post(
        "/api/execution/prepare",
        json=_spot_prepare_body(),
        headers=_auth(user),
    )
    order_id = prep.json()["order_id"]
    order = await db_session.get(Order, order_id)
    order.status = OrderStatus.CANCELLED.value
    await db_session.flush()
    r = await client.post(
        f"/api/execution/prepare-cancel/{order_id}",
        headers=_auth(user),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["replayed"] is True
    assert body["local_only"] is True


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


async def test_list_orders_returns_only_users_own(app_and_client, db_session):
    _, client = app_and_client
    await _seed_btc_spot_symbol(db_session)
    user_a = await _seed_user(db_session)
    user_b = await _seed_user(db_session)
    # User A creates an order via prepare
    await client.post(
        "/api/execution/prepare",
        json=_spot_prepare_body(),
        headers=_auth(user_a),
    )
    # User B's list is empty
    r = await client.get("/api/execution/orders", headers=_auth(user_b))
    assert r.status_code == 200
    body = r.json()
    assert body["items"] == []
    assert body["total"] == 0


async def test_list_orders_filters_by_status_and_venue(app_and_client, db_session):
    _, client = app_and_client
    await _seed_btc_spot_symbol(db_session)
    user = await _seed_user(db_session)
    prep = await client.post(
        "/api/execution/prepare",
        json=_spot_prepare_body(),
        headers=_auth(user),
    )
    order_id = prep.json()["order_id"]

    # Match
    r1 = await client.get(
        "/api/execution/orders?status=pending&venue=sodex_spot",
        headers=_auth(user),
    )
    assert r1.status_code == 200
    assert r1.json()["total"] == 1

    # Wrong status filter
    r2 = await client.get(
        "/api/execution/orders?status=filled",
        headers=_auth(user),
    )
    assert r2.json()["total"] == 0

    # Unknown status → 400
    r3 = await client.get(
        "/api/execution/orders?status=bogus",
        headers=_auth(user),
    )
    assert r3.status_code == 400

    # Unused order_id sanity
    assert order_id


async def test_list_orders_pagination(app_and_client, db_session):
    _, client = app_and_client
    await _seed_btc_spot_symbol(db_session)
    user = await _seed_user(db_session)
    # Create 3 orders
    for _ in range(3):
        await client.post(
            "/api/execution/prepare",
            json=_spot_prepare_body(),
            headers=_auth(user),
        )
    r = await client.get("/api/execution/orders?limit=2&offset=0", headers=_auth(user))
    assert r.json()["total"] == 3
    assert len(r.json()["items"]) == 2


async def test_get_order_happy_and_404_on_other(app_and_client, db_session):
    _, client = app_and_client
    await _seed_btc_spot_symbol(db_session)
    user_a = await _seed_user(db_session)
    user_b = await _seed_user(db_session)
    prep = await client.post(
        "/api/execution/prepare",
        json=_spot_prepare_body(),
        headers=_auth(user_a),
    )
    order_id = prep.json()["order_id"]
    r_a = await client.get(f"/api/execution/orders/{order_id}", headers=_auth(user_a))
    assert r_a.status_code == 200
    r_b = await client.get(f"/api/execution/orders/{order_id}", headers=_auth(user_b))
    assert r_b.status_code == 404


async def test_list_positions_open_only(app_and_client, db_session):
    _, client = app_and_client
    user = await _seed_user(db_session)
    # 2 positions: one OPEN, one CLOSED
    db_session.add_all(
        [
            Position(
                user_id=user.id,
                venue=Venue.SODEX_SPOT.value,
                asset="BTC",
                side="long",
                size=Decimal("0.5"),
                entry_price=Decimal("65000"),
                status=PositionStatus.OPEN.value,
                paper_trade=True,
                opened_at=datetime.now(UTC) - timedelta(hours=1),
            ),
            Position(
                user_id=user.id,
                venue=Venue.SODEX_SPOT.value,
                asset="ETH",
                side="long",
                size=Decimal("1"),
                entry_price=Decimal("3500"),
                status=PositionStatus.CLOSED.value,
                paper_trade=True,
                opened_at=datetime.now(UTC) - timedelta(days=1),
                closed_at=datetime.now(UTC),
            ),
        ]
    )
    await db_session.flush()
    r = await client.get("/api/execution/positions", headers=_auth(user))
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["asset"] == "BTC"
    assert items[0]["status"] == "open"


async def test_list_symbols_filter_by_venue(app_and_client, db_session):
    _, client = app_and_client
    user = await _seed_user(db_session)
    db_session.add_all(
        [
            SodexSymbol(
                venue=Venue.SODEX_SPOT.value,
                symbol_id=1,
                name="vBTC_vUSDC",
                asset="BTC",
                raw={},
                refreshed_at=datetime.now(UTC),
            ),
            SodexSymbol(
                venue=Venue.SODEX_PERPS.value,
                symbol_id=2,
                name="vETH_vUSDC",
                asset="ETH",
                raw={},
                refreshed_at=datetime.now(UTC),
            ),
        ]
    )
    await db_session.flush()
    r = await client.get(
        "/api/execution/symbols?venue=sodex_spot",
        headers=_auth(user),
    )
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["asset"] == "BTC"


async def test_list_symbols_empty_returns_200(app_and_client, db_session):
    """No symbols cached → empty list, not 404/503."""
    _, client = app_and_client
    user = await _seed_user(db_session)
    r = await client.get("/api/execution/symbols", headers=_auth(user))
    assert r.status_code == 200
    assert r.json()["items"] == []


# ---------------------------------------------------------------------------
# PR P1.4 — close-position route
# ---------------------------------------------------------------------------


async def _seed_btc_perps_symbol(db_session) -> None:
    db_session.add(
        SodexSymbol(
            venue=Venue.SODEX_PERPS.value,
            symbol_id=2,
            name="vBTC_vUSDC_PERP",
            asset="BTC",
            raw={"id": 2, "name": "vBTC_vUSDC_PERP"},
            refreshed_at=datetime.now(UTC),
        )
    )
    await db_session.flush()


async def _open_position(
    db_session,
    *,
    user: User,
    venue: str,
    side: str = "long",
    size: Decimal = Decimal("0.01"),
    entry_price: Decimal = Decimal("65000"),
    leverage: Decimal | None = None,
) -> Position:
    p = Position(
        user_id=user.id,
        venue=venue,
        asset="BTC",
        side=side,
        size=size,
        entry_price=entry_price,
        status=PositionStatus.OPEN.value,
        leverage=leverage,
    )
    db_session.add(p)
    await db_session.flush()
    return p


async def test_close_position_401_unauth(app_and_client):
    _, client = app_and_client
    r = await client.post("/api/execution/close-position/1")
    assert r.status_code == 401


async def test_close_position_404_nonexistent(app_and_client, db_session):
    _, client = app_and_client
    user = await _seed_user(db_session)
    r = await client.post("/api/execution/close-position/999999", headers=_auth(user))
    assert r.status_code == 404


async def test_close_position_404_other_user(app_and_client, db_session):
    _, client = app_and_client
    owner = await _seed_user(db_session)
    other = await _seed_user(db_session)
    p = await _open_position(db_session, user=other, venue=Venue.SODEX_SPOT.value)
    r = await client.post(f"/api/execution/close-position/{p.id}", headers=_auth(owner))
    assert r.status_code == 404


async def test_close_position_404_when_already_closed(app_and_client, db_session):
    _, client = app_and_client
    user = await _seed_user(db_session)
    p = await _open_position(db_session, user=user, venue=Venue.SODEX_SPOT.value)
    p.status = PositionStatus.CLOSED.value
    await db_session.flush()
    r = await client.post(f"/api/execution/close-position/{p.id}", headers=_auth(user))
    assert r.status_code == 404


async def test_close_position_happy_path_spot_long(app_and_client, db_session):
    _, client = app_and_client
    await _seed_btc_spot_symbol(db_session)
    user = await _seed_user(db_session)
    p = await _open_position(db_session, user=user, venue=Venue.SODEX_SPOT.value, side="long")
    r = await client.post(f"/api/execution/close-position/{p.id}", headers=_auth(user))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["order_id"]
    assert body["typed_data"]["primaryType"] == "ExchangeAction"
    # Confirm the order was inserted as opposite-side SELL MARKET on spot.
    order = await db_session.get(Order, body["order_id"])
    assert order is not None
    assert order.side == "sell"
    assert order.order_type == "market"
    assert order.reduce_only is False  # spot can't reduce_only
    assert order.requested_size == p.size


async def test_close_position_happy_path_perps_long(app_and_client, db_session):
    _, client = app_and_client
    await _seed_btc_perps_symbol(db_session)
    user = await _seed_user(db_session)
    p = await _open_position(
        db_session,
        user=user,
        venue=Venue.SODEX_PERPS.value,
        side="long",
        leverage=Decimal("3"),
    )
    r = await client.post(f"/api/execution/close-position/{p.id}", headers=_auth(user))
    assert r.status_code == 200, r.text
    order = await db_session.get(Order, r.json()["order_id"])
    assert order is not None
    assert order.side == "sell"
    assert order.order_type == "market"
    assert order.reduce_only is True
    # Leverage forwards into EIP-712 typed-data via RiskRequest; the
    # Order row doesn't persist leverage (lives on Position only).
    assert order.venue == Venue.SODEX_PERPS.value


async def test_close_position_perps_null_leverage_is_closable(app_and_client, db_session):
    # Regression: a perps position opened before leverage was mandatory
    # carries NULL leverage. The close route must still succeed — leverage
    # is immaterial to a reduce-only close, so it falls back to 1× rather
    # than failing the `perps_leverage_missing` risk gate (which would make
    # the held position permanently un-closable).
    _, client = app_and_client
    await _seed_btc_perps_symbol(db_session)
    user = await _seed_user(db_session)
    p = await _open_position(
        db_session,
        user=user,
        venue=Venue.SODEX_PERPS.value,
        side="long",
        leverage=None,
    )
    r = await client.post(f"/api/execution/close-position/{p.id}", headers=_auth(user))
    assert r.status_code == 200, r.text
    order = await db_session.get(Order, r.json()["order_id"])
    assert order is not None
    assert order.side == "sell"
    assert order.reduce_only is True


async def test_close_position_happy_path_perps_short(app_and_client, db_session):
    _, client = app_and_client
    await _seed_btc_perps_symbol(db_session)
    user = await _seed_user(db_session)
    p = await _open_position(
        db_session,
        user=user,
        venue=Venue.SODEX_PERPS.value,
        side="short",
        leverage=Decimal("3"),
    )
    r = await client.post(f"/api/execution/close-position/{p.id}", headers=_auth(user))
    assert r.status_code == 200, r.text
    order = await db_session.get(Order, r.json()["order_id"])
    assert order is not None
    assert order.side == "buy"  # opposite of short
    assert order.reduce_only is True


async def test_close_position_503_when_price_unavailable(app_and_client, db_session, monkeypatch):
    """When the spot-price oracle returns None, surface 503 with operator hint."""
    _, client = app_and_client
    await _seed_btc_spot_symbol(db_session)
    user = await _seed_user(db_session)
    p = await _open_position(db_session, user=user, venue=Venue.SODEX_SPOT.value)

    async def _no_price(asset):
        return None

    monkeypatch.setattr("etfpulse.api.routes.execution.get_spot_price_with_source", _no_price)
    r = await client.post(f"/api/execution/close-position/{p.id}", headers=_auth(user))
    assert r.status_code == 503
    assert "price unavailable" in r.json()["detail"]


# ---------------------------------------------------------------------------
# PR P1-fix.B2 — close-position perps dedupe
# ---------------------------------------------------------------------------


async def test_close_position_perps_409_when_close_already_in_flight(app_and_client, db_session):
    """Two consecutive close-position POSTs on a perps position: the
    2nd MUST 409 with the in-flight order_id rather than create a 2nd
    reduce_only order that would queue indefinitely."""
    _, client = app_and_client
    await _seed_btc_perps_symbol(db_session)
    user = await _seed_user(db_session)
    pos = await _open_position(
        db_session,
        user=user,
        venue=Venue.SODEX_PERPS.value,
        side="long",
        leverage=Decimal("3"),
    )
    r1 = await client.post(f"/api/execution/close-position/{pos.id}", headers=_auth(user))
    assert r1.status_code == 200, r1.text
    first_close_id = r1.json()["order_id"]

    r2 = await client.post(f"/api/execution/close-position/{pos.id}", headers=_auth(user))
    assert r2.status_code == 409, r2.text
    body = r2.json()
    assert body["detail"]["reason"] == "close_already_in_flight"
    assert body["detail"]["order_id"] == first_close_id


async def test_close_position_perps_abandoned_close_does_not_block(app_and_client, db_session):
    """PR P1 review P1 fix — the dedupe is WINDOWED. An immediate close that
    was prepared but never signed + submitted (an abandoned non-terminal
    PENDING) older than `execution_close_dedupe_window_seconds` must NOT
    block a fresh close — otherwise the position is un-closable for the full
    24h nonce window. Such an order never reached the venue, so there's no
    double-execution risk."""
    _, client = app_and_client
    await _seed_btc_perps_symbol(db_session)
    user = await _seed_user(db_session)
    pos = await _open_position(
        db_session,
        user=user,
        venue=Venue.SODEX_PERPS.value,
        side="long",
        leverage=Decimal("3"),
    )
    # An abandoned immediate close from an hour ago — non-terminal,
    # reduce_only, non-conditional, unsigned, well outside the 120s window.
    abandoned = Order(
        user_id=user.id,
        client_order_id="abandoned-close-1",
        venue=Venue.SODEX_PERPS.value,
        asset="BTC",
        side="sell",
        order_type="market",
        time_in_force="ioc",
        requested_size=Decimal("0.01"),
        requested_price=Decimal("65000"),
        status=OrderStatus.PENDING.value,
        reduce_only=True,
        is_conditional=False,
        created_at=datetime.now(UTC) - timedelta(hours=1),
    )
    db_session.add(abandoned)
    await db_session.flush()

    r = await client.post(f"/api/execution/close-position/{pos.id}", headers=_auth(user))
    assert r.status_code == 200, r.text


async def test_close_position_perps_allowed_with_resting_sl(app_and_client, db_session):
    """PR P1-fix.Issue-A: a resting SL/TP is `reduce_only=True` but
    `is_conditional=True`. The B2 dedupe matches only IMMEDIATE closes
    (`is_conditional=False`), so a manual close on a protected position
    MUST still succeed (200), not 409."""
    _, client = app_and_client
    await _seed_btc_perps_symbol(db_session)
    user = await _seed_user(db_session)
    pos = await _open_position(
        db_session,
        user=user,
        venue=Venue.SODEX_PERPS.value,
        side="long",
        leverage=Decimal("3"),
    )
    # Insert a resting conditional SL: opposite-side, reduce_only,
    # is_conditional, ACKED on the venue.
    resting_sl = Order(
        user_id=user.id,
        client_order_id="resting-sl-1",
        venue=Venue.SODEX_PERPS.value,
        asset="BTC",
        side="sell",
        order_type="market",
        time_in_force="ioc",
        requested_size=Decimal("0.01"),
        requested_price=Decimal("65000"),
        status=OrderStatus.ACKED.value,
        reduce_only=True,
        is_conditional=True,
        stop_price=Decimal("60000"),
        stop_type="stop_loss",
    )
    db_session.add(resting_sl)
    await db_session.flush()

    r = await client.post(f"/api/execution/close-position/{pos.id}", headers=_auth(user))
    assert r.status_code == 200, r.text
    # And the immediate close just created IS picked up by the dedupe
    # (is_conditional=False) — a 2nd close now 409s.
    r2 = await client.post(f"/api/execution/close-position/{pos.id}", headers=_auth(user))
    assert r2.status_code == 409, r2.text


async def test_close_position_spot_does_not_dedupe(app_and_client, db_session):
    """Spot SELLs are indistinguishable from regular trades (no
    reduce_only marker on spot). The dedupe MUST be skipped — the
    venue rejects oversell. Test pins the asymmetry so a future
    "add spot dedupe" change is deliberate, not silent."""
    _, client = app_and_client
    await _seed_btc_spot_symbol(db_session)
    user = await _seed_user(db_session)
    pos = await _open_position(
        db_session,
        user=user,
        venue=Venue.SODEX_SPOT.value,
        side="long",
    )
    r1 = await client.post(f"/api/execution/close-position/{pos.id}", headers=_auth(user))
    assert r1.status_code == 200
    r2 = await client.post(f"/api/execution/close-position/{pos.id}", headers=_auth(user))
    # 2nd call succeeds (creates a 2nd close order); spot has no
    # in-DB dedupe. Documented behavior — gateway oversell is the
    # downstream guard.
    assert r2.status_code == 200


# ---------------------------------------------------------------------------
# PR P1-fix.F1 — true concurrency assertion on close-position
# ---------------------------------------------------------------------------


async def test_close_position_perps_concurrent_requests_exactly_one_wins(test_engine):
    """Fire two close-position POSTs in true parallel (asyncio.gather)
    with per-request DB sessions. The User-row FOR UPDATE lock should
    serialise them: exactly one returns 200 (creating the close), the
    other returns 409 (dedupe finds the first's close).

    Without the F1 lock, both POSTs would pass the dedupe SELECT in
    their own transactions before either commits, and both would
    insert close orders — surfaceable as `assert sorted(codes) ==
    [200, 200]` failing this test."""
    import asyncio

    from etfpulse.adapters.sodex.perps_client import SodexPerpsClient
    from etfpulse.adapters.sodex.spot_client import SodexSpotClient

    # Seed user + position + symbol in a committed setup session so
    # both racing requests can see them.
    sessionmaker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with sessionmaker() as setup:
        u = User(
            wallet_address=_wallet(),
            sodex_account_id=57436,
            sodex_spot_api_key_name="default",
            sodex_perps_api_key_name="default",
            paper_trade=True,
        )
        setup.add(u)
        await setup.flush()
        user_id = u.id
        setup.add(
            SodexSymbol(
                venue=Venue.SODEX_PERPS.value,
                symbol_id=2,
                name="vBTC_vUSDC_PERP",
                asset="BTC",
                raw={"id": 2, "name": "vBTC_vUSDC_PERP"},
                refreshed_at=datetime.now(UTC),
            )
        )
        p = Position(
            user_id=user_id,
            venue=Venue.SODEX_PERPS.value,
            asset="BTC",
            side="long",
            size=Decimal("0.01"),
            entry_price=Decimal("65000"),
            status=PositionStatus.OPEN.value,
            leverage=Decimal("3"),
        )
        setup.add(p)
        await setup.commit()
        position_id = p.id

    # Build an app whose get_db_session yields a FRESH session per
    # request (production-shaped, not the shared-session test default).
    app = create_app()

    async def _per_request_session() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as s:
            yield s

    app.dependency_overrides[get_db_session] = _per_request_session
    ok_item = OrderResponseItem(code=0, orderID=12345, clOrdID="cli-xyz")
    spot = AsyncMock(spec=SodexSpotClient)
    spot.submit_batch_new_order = AsyncMock(return_value=[ok_item])
    perps = AsyncMock(spec=SodexPerpsClient)
    perps.submit_batch_new_order = AsyncMock(return_value=[ok_item])
    app.state.sodex_spot_client = spot
    app.state.sodex_perps_client = perps

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            headers = {"Authorization": f"Bearer {mint_jwt(user_id)}"}
            url = f"/api/execution/close-position/{position_id}"
            r1, r2 = await asyncio.gather(
                client.post(url, headers=headers),
                client.post(url, headers=headers),
            )
        codes = sorted([r1.status_code, r2.status_code])
        assert codes == [200, 409], (
            f"expected [200, 409] (lock serialises, dedupe blocks 2nd) — "
            f"got {codes}. r1={r1.text} r2={r2.text}"
        )
        # The 409 carries the in-flight order_id from the 200 winner.
        winner = r1 if r1.status_code == 200 else r2
        loser = r2 if r1.status_code == 200 else r1
        assert loser.json()["detail"]["reason"] == "close_already_in_flight"
        assert loser.json()["detail"]["order_id"] == winner.json()["order_id"]
    finally:
        # Clean up the committed rows so they don't leak between tests.
        async with sessionmaker() as cleanup:
            await cleanup.execute(Order.__table__.delete().where(Order.user_id == user_id))
            await cleanup.execute(Position.__table__.delete().where(Position.id == position_id))
            await cleanup.execute(
                SodexSymbol.__table__.delete().where(SodexSymbol.venue == Venue.SODEX_PERPS.value)
            )
            await cleanup.execute(User.__table__.delete().where(User.id == user_id))
            await cleanup.commit()
