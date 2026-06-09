"""Tests for GET /api/execution/limits (P0) and
GET /api/execution/account-summary (P1/P2).

`/limits` is DB-only — caps from settings + usage from `compute_usage`.
`/account-summary` aggregates SoDEX reads (balances + fee + perps mark
prices), parallel, with the bootstrap route's 404→empty / SodexError→503
degrade semantics.

Pinned behaviors:
  /limits
    - 401 unauthed; 403 wallet-unbound (get_current_user gate).
    - 200 clean user → caps echo settings, usage zero, per_symbol null w/o asset.
    - 200 with seeded orders + ?asset=btc → usage reflects orders; asset
      normalised to uppercase; per_symbol scoped to the asset.
  /account-summary
    - 401 unauthed; 503 when clients missing on app.state.
    - 200 happy → balances mapped (available = total − locked), fee mapped,
      mark price asset extracted from the venue symbol ("BTC-USD" → "BTC").
    - 404 on a sub-call → that field empty/None (not a whole-route failure).
    - non-404 SodexError → 503.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from decimal import Decimal

import httpx
import pytest
from fastapi import FastAPI

from etfpulse.adapters.sodex._http import SodexHttpError, SodexRateLimitError
from etfpulse.adapters.sodex.responses import (
    AccountBalances,
    FeeRate,
    PerpsMarkPrice,
)
from etfpulse.api.auth import mint_jwt
from etfpulse.api.deps import get_db_session, get_sodex_clients
from etfpulse.app import create_app
from etfpulse.config import settings
from etfpulse.models import Order, User
from etfpulse.models.order import OrderSide as DbOrderSide
from etfpulse.models.order import OrderStatus, Venue
from etfpulse.models.order import OrderType as DbOrderType
from etfpulse.models.order import TimeInForce as DbTimeInForce

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fakes — only the methods these routes reach are implemented.
# ---------------------------------------------------------------------------


def _balances(*rows: tuple[str, str, str]) -> AccountBalances:
    """rows = (asset, total, locked)."""
    return AccountBalances.model_validate(
        {
            "data": None,
            "balances": [
                {"i": i, "a": a, "t": t, "l": lk} for i, (a, t, lk) in enumerate(rows, start=1)
            ],
            "blockHeight": 1,
            "blockTime": 1,
        }
    )


def _fee(maker: str, taker: str) -> FeeRate:
    return FeeRate.model_validate(
        {
            "feeTier": 0,
            "makerFeeRate": maker,
            "takerFeeRate": taker,
            "makerRebateTier": 0,
            "stakingTier": 0,
        }
    )


def _mark(symbol: str, mark: str, funding: str = "0.0001") -> PerpsMarkPrice:
    return PerpsMarkPrice.model_validate(
        {
            "symbol": symbol,
            "markPrice": mark,
            "indexPrice": mark,
            "fundingRate": funding,
            "nextFundingTime": 1_700_000_000_000,
            "openInterest": "0",
        }
    )


class _FakeSpot:
    def __init__(self, *, balances=None, fee=None):
        self._balances = balances if balances is not None else _balances(("USDC", "1000", "100"))
        self._fee = fee if fee is not None else _fee("0.0002", "0.0005")

    async def get_balances(self, address: str):
        if isinstance(self._balances, Exception):
            raise self._balances
        return self._balances

    async def get_fee_rate(self, address: str):
        if isinstance(self._fee, Exception):
            raise self._fee
        return self._fee


class _FakePerps:
    def __init__(self, *, marks=None):
        self._marks = marks if marks is not None else [_mark("BTC-USD", "65000")]

    async def get_mark_prices(self):
        if isinstance(self._marks, Exception):
            raise self._marks
        return self._marks


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def app(db_session) -> AsyncIterator[FastAPI]:
    a = create_app()

    async def _override_session():
        yield db_session

    a.dependency_overrides[get_db_session] = _override_session
    yield a


def _install_clients(app: FastAPI, *, spot=None, perps=None) -> None:
    if spot is None and perps is None:
        app.dependency_overrides.pop(get_sodex_clients, None)
        return
    app.dependency_overrides[get_sodex_clients] = lambda: (spot, perps)


async def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _user(db_session, *, bound: bool = True) -> User:
    u = User(
        wallet_address=("0x" + secrets.token_hex(20)) if bound else None,
        sodex_account_id=1 if bound else None,
    )
    db_session.add(u)
    await db_session.flush()
    return u


def _auth(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {mint_jwt(user.id)}"}


_COID = iter(range(1, 1_000_000))


async def _order(db_session, *, user_id, size, price, asset="BTC", status=OrderStatus.ACKED.value):
    o = Order(
        user_id=user_id,
        client_order_id=f"co-{next(_COID)}",
        venue=Venue.SODEX_SPOT.value,
        asset=asset,
        side=DbOrderSide.BUY.value,
        order_type=DbOrderType.LIMIT.value,
        time_in_force=DbTimeInForce.GTC.value,
        requested_size=Decimal(size),
        requested_price=Decimal(price),
        status=status,
    )
    db_session.add(o)
    await db_session.flush()
    return o


# ---------------------------------------------------------------------------
# /limits
# ---------------------------------------------------------------------------


async def test_limits_401_unauthed(app):
    async with await _client(app) as c:
        r = await c.get("/api/execution/limits")
    assert r.status_code == 401


async def test_limits_403_wallet_unbound(app, db_session):
    u = await _user(db_session, bound=False)
    async with await _client(app) as c:
        r = await c.get("/api/execution/limits", headers=_auth(u))
    assert r.status_code == 403


async def test_limits_clean_user_echoes_caps_zero_usage(app, db_session):
    u = await _user(db_session)
    async with await _client(app) as c:
        r = await c.get("/api/execution/limits", headers=_auth(u))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["max_open_orders"] == settings.execution_max_open_orders_per_user
    assert body["max_leverage"] == settings.execution_max_leverage
    # Decimal → JSON string (OrderOut convention).
    assert body["daily_notional_cap"] == str(settings.execution_daily_notional_usd_cap)
    assert body["per_symbol_cap"] == str(settings.execution_per_symbol_notional_usd_cap)
    assert body["open_orders_used"] == 0
    assert Decimal(body["daily_notional_used"]) == Decimal("0")
    # No asset queried → per_symbol_used null, asset null.
    assert body["per_symbol_used"] is None
    assert body["asset"] is None


async def test_limits_with_orders_and_asset(app, db_session):
    u = await _user(db_session)
    await _order(db_session, user_id=u.id, size="0.01", price="65000", asset="BTC")  # 650
    await _order(db_session, user_id=u.id, size="1", price="3000", asset="ETH")  # 3000
    async with await _client(app) as c:
        r = await c.get("/api/execution/limits", headers=_auth(u), params={"asset": "btc"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["open_orders_used"] == 2
    assert Decimal(body["daily_notional_used"]) == Decimal("3650")  # both
    assert Decimal(body["per_symbol_used"]) == Decimal("650")  # BTC only
    assert body["asset"] == "BTC"  # normalised uppercase


# ---------------------------------------------------------------------------
# /account-summary
# ---------------------------------------------------------------------------


async def test_account_summary_401_unauthed(app):
    async with await _client(app) as c:
        r = await c.get("/api/execution/account-summary")
    assert r.status_code == 401


async def test_account_summary_503_clients_missing(app, db_session):
    u = await _user(db_session)
    _install_clients(app, spot=None, perps=None)  # no override → real dep → app.state empty
    async with await _client(app) as c:
        r = await c.get("/api/execution/account-summary", headers=_auth(u))
    assert r.status_code == 503


async def test_account_summary_happy(app, db_session):
    u = await _user(db_session)
    _install_clients(
        app,
        spot=_FakeSpot(
            balances=_balances(("USDC", "1000", "100"), ("BTC", "0.5", "0")),
            fee=_fee("0.0002", "0.0005"),
        ),
        perps=_FakePerps(marks=[_mark("BTC-USD", "65000", "0.0001"), _mark("ETH-USD", "3000")]),
    )
    async with await _client(app) as c:
        r = await c.get("/api/execution/account-summary", headers=_auth(u))
    assert r.status_code == 200, r.text
    body = r.json()
    usdc = next(b for b in body["spot_balances"] if b["asset"] == "USDC")
    assert Decimal(usdc["total"]) == Decimal("1000")
    assert Decimal(usdc["locked"]) == Decimal("100")
    assert Decimal(usdc["available"]) == Decimal("900")  # total - locked
    assert Decimal(body["fee"]["taker_rate"]) == Decimal("0.0005")
    btc_mark = next(m for m in body["mark_prices"] if m["symbol"] == "BTC-USD")
    assert btc_mark["asset"] == "BTC"  # extracted from BASE-USD
    assert Decimal(btc_mark["mark_price"]) == Decimal("65000")


async def test_account_summary_404_degrades_to_empty(app, db_session):
    u = await _user(db_session)
    _install_clients(
        app,
        spot=_FakeSpot(
            balances=SodexHttpError("no account", status_code=404),
            fee=SodexHttpError("no fee tier", status_code=404),
        ),
        perps=_FakePerps(marks=[_mark("BTC-USD", "65000")]),
    )
    async with await _client(app) as c:
        r = await c.get("/api/execution/account-summary", headers=_auth(u))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["spot_balances"] == []
    assert body["fee"] is None
    assert len(body["mark_prices"]) == 1


async def test_account_summary_503_on_sodex_error(app, db_session):
    u = await _user(db_session)
    _install_clients(
        app,
        spot=_FakeSpot(balances=SodexRateLimitError("rate limited")),
        perps=_FakePerps(),
    )
    async with await _client(app) as c:
        r = await c.get("/api/execution/account-summary", headers=_auth(u))
    assert r.status_code == 503
    assert r.json()["detail"] == "account_summary_unavailable"


async def test_account_summary_503_on_unexpected_error(app, db_session):
    """A NON-SodexError escape (e.g. a response-shape ValidationError or a
    Decimal parse on an unexpected value) must degrade to 503, never a 500 —
    the FE treats 503 as 'summary unavailable' and degrades gracefully."""
    u = await _user(db_session)
    _install_clients(
        app,
        spot=_FakeSpot(balances=ValueError("unexpected response shape")),
        perps=_FakePerps(),
    )
    async with await _client(app) as c:
        r = await c.get("/api/execution/account-summary", headers=_auth(u))
    assert r.status_code == 503
    assert r.json()["detail"] == "account_summary_unavailable"
