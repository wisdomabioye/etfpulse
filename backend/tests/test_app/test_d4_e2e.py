"""PR D.4 end-to-end integration test.

Walks the full user journey across D.4.1 / D.4.2 / D.4.3 surfaces in
one test to catch regressions where the per-task units pass but the
seams between them break:

  1. POST /api/wallet/nonce       — SIWE nonce issued.
  2. POST /api/wallet/verify      — sign + verify → JWT + new User row.
  3. POST /api/wallet/api-key     — bind a SoDEX api key name for spot.
  4. POST /api/execution/prepare  — risk check + INSERT order + EIP-712
                                     typed-data returned.
  5. POST /api/execution/submit   — paper-trade short-circuit (User
                                     has paper_trade=True), order
                                     transitions to FILLED.
  6. GET  /api/execution/orders   — order visible in user's list.
  7. GET  /api/execution/positions — position created by the paper fill.

Pinned end-to-end because each unit test mocks its neighbours; this
one runs the real backend pipeline (with SIWE crypto via eth_account
+ paper-trade execution path) against the test DB.

Same `eth_account` carve-out as `test_wallet_routes.py` — backend
NEVER signs in production; the test fixture signs locally to drive
the verifier.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import httpx
import pytest
from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi import FastAPI
from siwe import SiweMessage
from sqlalchemy.ext.asyncio import AsyncSession

from etfpulse.adapters.sodex.perps_client import SodexPerpsClient
from etfpulse.adapters.sodex.responses import OrderResponseItem
from etfpulse.adapters.sodex.spot_client import SodexSpotClient
from etfpulse.api import auth_siwe as siwe_mod
from etfpulse.api.deps import get_db_session
from etfpulse.app import create_app
from etfpulse.config import settings
from etfpulse.models import SodexSymbol, User, Venue


@pytest.fixture(autouse=True)
def _reset_siwe_state(monkeypatch):
    monkeypatch.setattr(settings, "frontend_url", "https://etfpulse.example.com")
    monkeypatch.setattr(settings, "sodex_environment", "testnet")
    monkeypatch.setattr(settings, "wallet_nonce_ttl_seconds", 600)
    siwe_mod.reset_nonce_cache_for_tests()
    yield
    siwe_mod.reset_nonce_cache_for_tests()


@pytest.fixture
async def app_and_client(
    db_session: AsyncSession,
) -> AsyncIterator[tuple[FastAPI, httpx.AsyncClient]]:
    """Real FastAPI app + DB-session override + mock SoDEX clients on app.state."""
    app = create_app()

    async def _override_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db_session] = _override_session
    # Mock SoDEX clients — paper-trade path doesn't need them but the
    # submit route fetches them defensively.
    spot = AsyncMock(spec=SodexSpotClient)
    spot.submit_batch_new_order = AsyncMock(
        return_value=[OrderResponseItem(code=0, orderID=42, clOrdID="cli")]
    )
    perps = AsyncMock(spec=SodexPerpsClient)
    app.state.sodex_spot_client = spot
    app.state.sodex_perps_client = perps

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as c:
        yield app, c


def _sign_siwe(account, nonce: str) -> tuple[str, str]:
    """Synthesize + sign a SIWE message. Backend treats it like a
    real wallet response."""
    msg = SiweMessage(
        domain="etfpulse.example.com",
        address=account.address,
        statement=settings.siwe_statement,
        uri="https://etfpulse.example.com",
        version="1",
        chain_id=settings.sodex_chain_id,
        nonce=nonce,
        issued_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
    )
    prepared = msg.prepare_message()
    sig = account.sign_message(encode_defunct(text=prepared)).signature
    return prepared, "0x" + sig.hex()


async def test_d4_full_journey_paper_trade(app_and_client, db_session):
    """End-to-end: SIWE bind → api-key bind → prepare → submit (paper) → reads."""
    _, client = app_and_client

    # ---- Pre-seed the symbol cache. In prod the cron populates it;
    # tests need it pre-populated for `resolve_symbol_id` to find a row.
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

    # ---- 1. Nonce
    burner = Account.create()
    nonce_resp = await client.post(
        "/api/wallet/nonce",
        json={"address": burner.address},
    )
    assert nonce_resp.status_code == 200, nonce_resp.text
    nonce = nonce_resp.json()["nonce"]

    # ---- 2. Verify (sign + post)
    message, signature = _sign_siwe(burner, nonce)
    verify_resp = await client.post(
        "/api/wallet/verify",
        json={"message": message, "signature": signature},
    )
    assert verify_resp.status_code == 200, verify_resp.text
    jwt = verify_resp.json()["jwt"]
    user_id = verify_resp.json()["user_id"]
    headers = {"Authorization": f"Bearer {jwt}"}

    # User row was created with lowercased wallet
    user = await db_session.get(User, user_id)
    assert user is not None
    assert user.wallet_address == burner.address.lower()

    # ---- Flip paper_trade=True so submit short-circuits the gateway
    # call. In production the operator does this via the admin route.
    user.paper_trade = True
    await db_session.flush()

    # ---- 3. Bind a SoDEX api-key name + account_id for spot
    key_resp = await client.post(
        "/api/wallet/api-key",
        json={
            "venue": "sodex_spot",
            "api_key_name": "default",
            "sodex_account_id": 57436,
        },
        headers=headers,
    )
    assert key_resp.status_code == 200, key_resp.text

    # ---- 4. Prepare new order
    prepare_resp = await client.post(
        "/api/execution/prepare",
        json={
            "venue": "sodex_spot",
            "asset": "BTC",
            "side": "buy",
            "order_type": "limit",
            "time_in_force": "gtc",
            "requested_size": "0.01",
            "requested_price": "65000",
        },
        headers=headers,
    )
    assert prepare_resp.status_code == 200, prepare_resp.text
    prepare_body = prepare_resp.json()
    order_id = prepare_body["order_id"]
    assert prepare_body["typed_data"]["primaryType"] == "ExchangeAction"

    # ---- 5. Submit (paper-trade short-circuit)
    # Signature shape only — paper-trade path doesn't validate the
    # signature cryptographically; just the shape.
    fake_sig = "0x01" + secrets.token_hex(65)
    submit_resp = await client.post(
        f"/api/execution/submit/{order_id}",
        json={"signature": fake_sig},
        headers=headers,
    )
    assert submit_resp.status_code == 200, submit_resp.text
    submit_body = submit_resp.json()
    assert submit_body["order_id"] == order_id
    assert submit_body["status"] in {"filled", "acked"}

    # ---- 6. Order visible in user's list
    orders_resp = await client.get("/api/execution/orders", headers=headers)
    assert orders_resp.status_code == 200
    orders = orders_resp.json()
    assert orders["total"] >= 1
    matching = [o for o in orders["items"] if o["id"] == order_id]
    assert len(matching) == 1
    assert matching[0]["paper_trade"] is True

    # ---- 7. Position created by paper fill
    positions_resp = await client.get("/api/execution/positions", headers=headers)
    assert positions_resp.status_code == 200
    positions = positions_resp.json()["items"]
    assert len(positions) >= 1
    assert any(p["asset"] == "BTC" and p["paper_trade"] is True for p in positions)
