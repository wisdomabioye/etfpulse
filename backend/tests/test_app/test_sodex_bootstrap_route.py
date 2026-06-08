"""Tests for GET /api/wallet/sodex-bootstrap (SDXB.2 / task #212).

The bootstrap route auto-fetches the wallet's SoDEX `account_id` and
named API keys from the gateway so the FE skips the manual form.

Pinned behaviors:
  - 401 unauthed.
  - 403 when wallet not bound (get_current_user gate).
  - 503 when SoDEX clients not initialised on app.state.
  - 200 happy: one key on each venue + account_id from spot state.
  - 200 multi-key: 2+ keys returned per venue (FE renders dropdown).
  - 200 no SoDEX account: get_state 404 → account_id=null; keys still
    return [] (404 from get_api_keys is also treated as zero keys).
  - 503 on any non-404 SodexError (rate limit, 5xx, parse error).
  - Parallel calls: the three SoDEX calls fire concurrently via
    asyncio.gather (verified via timing).

Mocks the long-lived clients by stamping fakes onto `app.state` after
`create_app()` (same access pattern `get_sodex_clients` dep reads).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from time import perf_counter

import httpx
import pytest
from fastapi import FastAPI

from etfpulse.adapters.sodex._http import (
    SodexEnvelopeError,
    SodexHttpError,
    SodexRateLimitError,
)
from etfpulse.adapters.sodex.responses import APIKey, BalanceEntry, SpotAccountState
from etfpulse.api.auth import mint_jwt
from etfpulse.api.deps import get_db_session, get_sodex_clients
from etfpulse.app import create_app
from etfpulse.models.user import User

# ---------------------------------------------------------------------------
# Fakes — stand in for the long-lived SoDEX HTTP clients on app.state.
# Only the methods bootstrap reaches are implemented; everything else
# raises NotImplementedError so a test that drifts past the bootstrap
# scope fails loudly.
# ---------------------------------------------------------------------------


def _make_api_key(name: str) -> APIKey:
    """Construct a synthetic APIKey response. The `name` field is what
    eventually goes in the X-API-Key header; other fields are filler
    values that match the wire shape (publicKey/expiresAt aliases)."""
    return APIKey.model_validate(
        {
            "name": name,
            "type": "EVM",
            "publicKey": "0x" + "ab" * 33,  # 66-char placeholder
            "expiresAt": 0,
        }
    )


def _make_spot_state(aid: int) -> SpotAccountState:
    return SpotAccountState.model_validate(
        {
            "user": "0x" + "cd" * 20,
            "aid": aid,
            "uid": 1,
            "B": [
                BalanceEntry.model_validate(
                    {"i": 1, "a": "USDT", "t": "0", "l": "0"}
                )
            ],
            "O": None,
        }
    )


class _FakeSpotClient:
    def __init__(
        self,
        *,
        state: SpotAccountState | Exception | None = None,
        keys: list[APIKey] | Exception | None = None,
        delay: float = 0.0,
    ):
        # Defaults — happy path: state with aid=42, empty keys list.
        self._state = state if state is not None else _make_spot_state(42)
        self._keys = keys if keys is not None else []
        self._delay = delay
        self.state_calls = 0
        self.keys_calls = 0

    async def get_state(self, address: str) -> SpotAccountState:
        self.state_calls += 1
        if self._delay:
            await asyncio.sleep(self._delay)
        if isinstance(self._state, Exception):
            raise self._state
        return self._state

    async def get_api_keys(self, address: str) -> list[APIKey]:
        self.keys_calls += 1
        if self._delay:
            await asyncio.sleep(self._delay)
        if isinstance(self._keys, Exception):
            raise self._keys
        return self._keys


class _FakePerpsClient:
    def __init__(
        self,
        *,
        keys: list[APIKey] | Exception | None = None,
        delay: float = 0.0,
    ):
        self._keys = keys if keys is not None else []
        self._delay = delay
        self.keys_calls = 0

    async def get_api_keys(self, address: str) -> list[APIKey]:
        self.keys_calls += 1
        if self._delay:
            await asyncio.sleep(self._delay)
        if isinstance(self._keys, Exception):
            raise self._keys
        return self._keys


# ---------------------------------------------------------------------------
# App + DB fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def app(db_session) -> AsyncIterator[FastAPI]:
    app = create_app()

    async def _override_session():
        yield db_session

    app.dependency_overrides[get_db_session] = _override_session
    yield app


def _install_clients(
    app: FastAPI,
    *,
    spot: _FakeSpotClient | None,
    perps: _FakePerpsClient | None,
) -> None:
    """Inject fakes via the `get_sodex_clients` dep override.

    Overriding the dep (rather than stamping on `app.state`) is the
    test-isolation-friendly path AND sidesteps the dep's isinstance
    check against the real `SodexSpotClient`/`SodexPerpsClient` types
    — the dep guarantees a `(spot, perps)` tuple, structural typing
    is enough for the route logic.

    When BOTH inputs are None, we DON'T override the dep — the route
    falls through to the real `get_sodex_clients`, which reads
    `app.state` (also not set in tests) → 503. That's the
    'scheduler-disabled' production path we want to exercise.
    """
    if spot is None and perps is None:
        app.dependency_overrides.pop(get_sodex_clients, None)
        return

    def _override():
        return (spot, perps)

    app.dependency_overrides[get_sodex_clients] = _override


@pytest.fixture
async def authed_client(
    app: FastAPI, db_session
) -> AsyncIterator[tuple[httpx.AsyncClient, User, dict[str, str]]]:
    """Build a bound-wallet user + JWT + a ready client."""
    u = User(wallet_address="0x" + "ab" * 20)
    db_session.add(u)
    await db_session.flush()
    token = mint_jwt(u.id)
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as c:
        yield c, u, headers


@pytest.fixture
async def unbound_client(
    app: FastAPI, db_session
) -> AsyncIterator[tuple[httpx.AsyncClient, User, dict[str, str]]]:
    """User with no wallet bound — exercises the 403 path."""
    u = User(wallet_address=None)
    db_session.add(u)
    await db_session.flush()
    token = mint_jwt(u.id)
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as c:
        yield c, u, headers


@pytest.fixture
async def anon_client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as c:
        yield c


# ---------------------------------------------------------------------------
# Auth gates
# ---------------------------------------------------------------------------


async def test_bootstrap_401_unauthed(app: FastAPI, anon_client):
    _install_clients(app, spot=_FakeSpotClient(), perps=_FakePerpsClient())
    r = await anon_client.get("/api/wallet/sodex-bootstrap")
    assert r.status_code == 401


async def test_bootstrap_403_wallet_unbound(app: FastAPI, unbound_client):
    _install_clients(app, spot=_FakeSpotClient(), perps=_FakePerpsClient())
    client, _, headers = unbound_client
    r = await client.get("/api/wallet/sodex-bootstrap", headers=headers)
    assert r.status_code == 403


async def test_bootstrap_503_when_clients_missing(app: FastAPI, authed_client):
    # Don't install clients — simulate scheduler-off / SODEX env unset.
    _install_clients(app, spot=None, perps=None)
    client, _, headers = authed_client
    r = await client.get("/api/wallet/sodex-bootstrap", headers=headers)
    assert r.status_code == 503
    assert "SoDEX clients not initialised" in r.json()["detail"]


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


async def test_bootstrap_200_single_key_per_venue(app: FastAPI, authed_client):
    spot = _FakeSpotClient(
        state=_make_spot_state(aid=42),
        keys=[_make_api_key("default-spot")],
    )
    perps = _FakePerpsClient(keys=[_make_api_key("default-perps")])
    _install_clients(app, spot=spot, perps=perps)

    client, _, headers = authed_client
    r = await client.get("/api/wallet/sodex-bootstrap", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["account_id"] == 42
    assert [k["name"] for k in body["spot_keys"]] == ["default-spot"]
    assert [k["name"] for k in body["perps_keys"]] == ["default-perps"]
    # Each upstream endpoint hit exactly once.
    assert spot.state_calls == 1
    assert spot.keys_calls == 1
    assert perps.keys_calls == 1


async def test_bootstrap_200_multi_key(app: FastAPI, authed_client):
    spot = _FakeSpotClient(
        keys=[_make_api_key("prod"), _make_api_key("dev"), _make_api_key("temp")],
    )
    perps = _FakePerpsClient(keys=[_make_api_key("prod"), _make_api_key("dev")])
    _install_clients(app, spot=spot, perps=perps)

    client, _, headers = authed_client
    r = await client.get("/api/wallet/sodex-bootstrap", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert [k["name"] for k in body["spot_keys"]] == ["prod", "dev", "temp"]
    assert [k["name"] for k in body["perps_keys"]] == ["prod", "dev"]


async def test_bootstrap_200_empty_keys(app: FastAPI, authed_client):
    """Wallet exists on SoDEX but has zero registered keys on either
    venue. Bootstrap returns account_id + empty lists; FE renders the
    'register a key' guidance per venue."""
    _install_clients(
        app,
        spot=_FakeSpotClient(state=_make_spot_state(aid=99), keys=[]),
        perps=_FakePerpsClient(keys=[]),
    )
    client, _, headers = authed_client
    r = await client.get("/api/wallet/sodex-bootstrap", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["account_id"] == 99
    assert body["spot_keys"] == []
    assert body["perps_keys"] == []


async def test_bootstrap_200_no_sodex_account(app: FastAPI, authed_client):
    """Wallet has never interacted with SoDEX → get_state returns 404
    → account_id=null. Keys also commonly 404 in this state; treated
    as empty list rather than 503."""
    _install_clients(
        app,
        spot=_FakeSpotClient(
            state=SodexHttpError("account not found", status_code=404),
            keys=SodexHttpError("no api keys", status_code=404),
        ),
        perps=_FakePerpsClient(keys=SodexHttpError("no api keys", status_code=404)),
    )
    client, _, headers = authed_client
    r = await client.get("/api/wallet/sodex-bootstrap", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["account_id"] is None
    assert body["spot_keys"] == []
    assert body["perps_keys"] == []


# ---------------------------------------------------------------------------
# SoDEX failures → 503
# ---------------------------------------------------------------------------


async def test_bootstrap_503_on_rate_limit(app: FastAPI, authed_client):
    _install_clients(
        app,
        spot=_FakeSpotClient(state=SodexRateLimitError("rate limited")),
        perps=_FakePerpsClient(),
    )
    client, _, headers = authed_client
    r = await client.get("/api/wallet/sodex-bootstrap", headers=headers)
    assert r.status_code == 503
    assert r.json()["detail"] == "sodex_bootstrap_unavailable"


async def test_bootstrap_503_on_5xx(app: FastAPI, authed_client):
    _install_clients(
        app,
        spot=_FakeSpotClient(),
        perps=_FakePerpsClient(
            keys=SodexHttpError("bad gateway", status_code=502),
        ),
    )
    client, _, headers = authed_client
    r = await client.get("/api/wallet/sodex-bootstrap", headers=headers)
    assert r.status_code == 503


async def test_bootstrap_503_on_envelope_error(app: FastAPI, authed_client):
    _install_clients(
        app,
        spot=_FakeSpotClient(
            keys=SodexEnvelopeError(
                "API key not found", code=-1, raw_error="API key not found"
            ),
        ),
        perps=_FakePerpsClient(),
    )
    client, _, headers = authed_client
    r = await client.get("/api/wallet/sodex-bootstrap", headers=headers)
    assert r.status_code == 503


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


async def test_bootstrap_calls_in_parallel(app: FastAPI, authed_client):
    """The 3 upstream calls run via asyncio.gather. If each fake sleeps
    100ms, total wall-clock should be ~100ms (parallel) NOT ~300ms
    (sequential). We assert under a generous ceiling to tolerate CI
    jitter while still catching a serialised regression."""
    delay = 0.1
    spot = _FakeSpotClient(delay=delay)
    perps = _FakePerpsClient(delay=delay)
    _install_clients(app, spot=spot, perps=perps)
    client, _, headers = authed_client

    t0 = perf_counter()
    r = await client.get("/api/wallet/sodex-bootstrap", headers=headers)
    elapsed = perf_counter() - t0

    assert r.status_code == 200
    # Sequential would be ~3*delay=0.3s; parallel ~delay=0.1s. 0.25s
    # ceiling catches the regression without flaking on CI jitter.
    assert elapsed < 0.25, f"bootstrap took {elapsed:.3f}s — calls likely serialised"
