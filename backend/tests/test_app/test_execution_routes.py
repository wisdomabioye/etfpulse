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
from sqlalchemy.ext.asyncio import AsyncSession

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
