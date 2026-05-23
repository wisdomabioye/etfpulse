"""Admin routes for the execution surface (PR D.3).

Routes covered:
  - POST /api/admin/execution/halt
  - POST /api/admin/execution/resume
  - POST /api/admin/users/{user_id}/paper-trade
  - POST /api/admin/sodex/symbols/refresh

Auth gates are the SAME `require_admin_key` chain as the existing admin
routes — verified in test_admin.py. We focus here on the route-specific
semantics + scope handling.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock

import httpx
import pytest
from httpx import ASGITransport
from sqlalchemy import select

from etfpulse.adapters.sodex.perps_client import SodexPerpsClient
from etfpulse.adapters.sodex.responses import PerpsSymbol, SpotSymbol
from etfpulse.adapters.sodex.spot_client import SodexSpotClient
from etfpulse.api.deps import get_db_session
from etfpulse.app import create_app
from etfpulse.config import settings
from etfpulse.models import SodexSymbol, User, Venue
from etfpulse.models.regime import CircuitBreaker, CircuitBreakerTrigger
from etfpulse.pipeline import circuit_breaker

_ADMIN_KEY = "secret-key"
_HEADERS = {"X-Admin-Key": _ADMIN_KEY}


@pytest.fixture
async def admin_client(db_session, monkeypatch):
    """Async client + admin auth + db_session override."""
    monkeypatch.setattr(settings, "admin_api_key", _ADMIN_KEY)
    app = create_app()

    async def _override() -> AsyncIterator:
        yield db_session

    app.dependency_overrides[get_db_session] = _override
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, app
    app.dependency_overrides.clear()


async def _seed_user(db_session, *, paper_trade: bool = False) -> User:
    u = User(wallet_address="0x" + secrets.token_hex(20), paper_trade=paper_trade)
    db_session.add(u)
    await db_session.flush()
    return u


# ---------------------------------------------------------------------------
# /admin/execution/halt
# ---------------------------------------------------------------------------


class TestExecutionHalt:
    async def test_global_halt_creates_breaker(self, admin_client, db_session):
        client, _ = admin_client
        resp = await client.post(
            "/api/admin/execution/halt",
            headers=_HEADERS,
            json={"reason": "manual ops halt"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["scope"] == "global"
        assert body["already_active"] is False
        assert body["breaker_id"] is not None

        # Breaker row persisted with user_id=NULL + details.
        rows = (await db_session.execute(select(CircuitBreaker))).scalars().all()
        assert len(rows) == 1
        assert rows[0].user_id is None
        assert rows[0].trigger_type == "manual"
        assert rows[0].details == {"reason": "manual ops halt"}

    async def test_per_user_halt_creates_scoped_breaker(self, admin_client, db_session):
        client, _ = admin_client
        user = await _seed_user(db_session)

        resp = await client.post(
            "/api/admin/execution/halt",
            headers=_HEADERS,
            json={"user_id": user.id, "reason": "loss limit"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["scope"] == "user"
        assert body["already_active"] is False

        rows = (await db_session.execute(select(CircuitBreaker))).scalars().all()
        assert len(rows) == 1
        assert rows[0].user_id == user.id

    async def test_idempotent_halt(self, admin_client, db_session):
        """Second halt with the same scope → already_active=True, no new row."""
        client, _ = admin_client
        first = await client.post(
            "/api/admin/execution/halt",
            headers=_HEADERS,
            json={"reason": "first"},
        )
        second = await client.post(
            "/api/admin/execution/halt",
            headers=_HEADERS,
            json={"reason": "second"},
        )
        assert first.status_code == 200
        assert second.status_code == 200
        # PR D.3.1 — idempotent noop now returns the EXISTING breaker's id +
        # triggered_at + details, not None.
        second_body = second.json()
        assert second_body["already_active"] is True
        assert second_body["breaker_id"] == first.json()["breaker_id"]
        assert second_body["existing_triggered_at"] is not None
        assert second_body["existing_details"] == {"reason": "first"}

        rows = (await db_session.execute(select(CircuitBreaker))).scalars().all()
        assert len(rows) == 1

    async def test_global_and_user_breakers_independent(self, admin_client, db_session):
        client, _ = admin_client
        user = await _seed_user(db_session)

        await client.post(
            "/api/admin/execution/halt",
            headers=_HEADERS,
            json={"reason": "global"},
        )
        await client.post(
            "/api/admin/execution/halt",
            headers=_HEADERS,
            json={"user_id": user.id, "reason": "scoped"},
        )

        rows = (await db_session.execute(select(CircuitBreaker))).scalars().all()
        scopes = sorted(r.user_id is None for r in rows)
        # One global (True), one per-user (False).
        assert scopes == [False, True]


# ---------------------------------------------------------------------------
# /admin/execution/resume
# ---------------------------------------------------------------------------


class TestExecutionResume:
    async def test_global_resume_clears_global_breaker(self, admin_client, db_session):
        client, _ = admin_client
        await circuit_breaker.record(db_session, CircuitBreakerTrigger.MANUAL.value)
        await db_session.commit()

        resp = await client.post(
            "/api/admin/execution/resume",
            headers=_HEADERS,
            json={},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["scope"] == "global"
        assert body["rowcount"] == 1

    async def test_resume_user_scope_does_not_clear_global(self, admin_client, db_session):
        """Resuming user A's breaker must NOT touch the global breaker —
        scopes are independent (per circuit_breaker.resolve contract)."""
        client, _ = admin_client
        user = await _seed_user(db_session)
        await circuit_breaker.record(db_session, CircuitBreakerTrigger.MANUAL.value)  # global
        await circuit_breaker.record(
            db_session, CircuitBreakerTrigger.MANUAL.value, user_id=user.id
        )
        await db_session.commit()

        resp = await client.post(
            "/api/admin/execution/resume",
            headers=_HEADERS,
            json={"user_id": user.id},
        )
        assert resp.status_code == 200
        assert resp.json()["rowcount"] == 1

        # Global still unresolved.
        active = (
            (
                await db_session.execute(
                    select(CircuitBreaker).where(
                        CircuitBreaker.user_id.is_(None),
                        CircuitBreaker.resolved_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(active) == 1

    async def test_resume_when_nothing_active_is_no_op(self, admin_client):
        client, _ = admin_client
        resp = await client.post(
            "/api/admin/execution/resume",
            headers=_HEADERS,
            json={},
        )
        assert resp.status_code == 200
        assert resp.json()["rowcount"] == 0


# ---------------------------------------------------------------------------
# /admin/users/{user_id}/paper-trade
# ---------------------------------------------------------------------------


class TestSetPaperTrade:
    async def test_flip_to_true(self, admin_client, db_session):
        client, _ = admin_client
        user = await _seed_user(db_session, paper_trade=False)

        resp = await client.post(
            f"/api/admin/users/{user.id}/paper-trade",
            headers=_HEADERS,
            json={"paper_trade": True},
        )
        assert resp.status_code == 200
        assert resp.json() == {"user_id": user.id, "paper_trade": True}

        await db_session.refresh(user)
        assert user.paper_trade is True

    async def test_unknown_user_404(self, admin_client):
        client, _ = admin_client
        resp = await client.post(
            "/api/admin/users/999999/paper-trade",
            headers=_HEADERS,
            json={"paper_trade": True},
        )
        assert resp.status_code == 404

    async def test_flip_to_false(self, admin_client, db_session):
        client, _ = admin_client
        user = await _seed_user(db_session, paper_trade=True)

        resp = await client.post(
            f"/api/admin/users/{user.id}/paper-trade",
            headers=_HEADERS,
            json={"paper_trade": False},
        )
        assert resp.status_code == 200
        await db_session.refresh(user)
        assert user.paper_trade is False


# ---------------------------------------------------------------------------
# /admin/users/{user_id}/unbind-wallet (#78.7)
# ---------------------------------------------------------------------------


class TestUnbindWallet:
    """Operator wallet-recovery endpoint. Clears all four wallet-bound
    fields atomically; leaves untouched everything else on the User row."""

    async def test_clears_all_four_wallet_bound_fields(self, admin_client, db_session):
        """Happy path — every wallet-bound field nulled in one call."""
        client, _ = admin_client
        user = User(
            wallet_address="0x" + secrets.token_hex(20),
            sodex_account_id=42,
            sodex_spot_api_key_name="my-spot",
            sodex_perps_api_key_name="my-perps",
            paper_trade=True,  # operator flag — MUST survive unbind
        )
        db_session.add(user)
        await db_session.flush()
        prev_wallet = user.wallet_address

        resp = await client.post(
            f"/api/admin/users/{user.id}/unbind-wallet",
            headers=_HEADERS,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body == {
            "user_id": user.id,
            "was_bound": True,
            "previous_wallet_address": prev_wallet,
        }

        await db_session.refresh(user)
        assert user.wallet_address is None
        assert user.sodex_account_id is None
        assert user.sodex_spot_api_key_name is None
        assert user.sodex_perps_api_key_name is None
        # paper_trade is operator-set, NOT wallet-derived — must survive.
        assert user.paper_trade is True

    async def test_idempotent_when_already_unbound(self, admin_client, db_session):
        """Re-unbinding an already-unbound user is a no-op (200, was_bound=false).
        Operators chaining recovery steps don't need to pre-check state."""
        client, _ = admin_client
        # User exists, never had a wallet bound.
        user = User(wallet_address=None)
        db_session.add(user)
        await db_session.flush()

        resp = await client.post(
            f"/api/admin/users/{user.id}/unbind-wallet",
            headers=_HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json() == {
            "user_id": user.id,
            "was_bound": False,
            "previous_wallet_address": None,
        }

    async def test_unknown_user_404(self, admin_client):
        """Missing user is 404 — distinguished from `was_bound=false` so
        operators can tell "user doesn't exist" from "user found, already
        unbound"."""
        client, _ = admin_client
        resp = await client.post(
            "/api/admin/users/999999/unbind-wallet",
            headers=_HEADERS,
        )
        assert resp.status_code == 404

    async def test_requires_admin_key(self, admin_client, db_session):
        """No `X-Admin-Key` header when the key IS configured → 401.
        (Empty-config posture is 503; the fixture configures a key, so
        missing-header is the "unauthorized" branch, not "disabled".)
        Pins the dependency wiring — the auth chain itself is covered
        in test_admin.py."""
        client, _ = admin_client
        user = await _seed_user(db_session)

        resp = await client.post(
            f"/api/admin/users/{user.id}/unbind-wallet",
        )
        assert resp.status_code == 401

    async def test_does_not_touch_unrelated_fields(self, admin_client, db_session):
        """User has delivery prefs (DeliveryPrefsMixin) + paper_trade
        operator flag. None should be affected. Pin this so a future
        drive-by edit doesn't accidentally widen the blast radius."""
        client, _ = admin_client
        user = User(
            wallet_address="0x" + secrets.token_hex(20),
            sodex_account_id=99,
            pref_assets=["BTC", "ETH"],
            pref_min_confidence=8,
            pref_paused=False,
            is_active=True,
            paper_trade=False,
        )
        db_session.add(user)
        await db_session.flush()

        resp = await client.post(
            f"/api/admin/users/{user.id}/unbind-wallet",
            headers=_HEADERS,
        )
        assert resp.status_code == 200

        await db_session.refresh(user)
        # Wallet-bound fields cleared.
        assert user.wallet_address is None
        assert user.sodex_account_id is None
        # Everything else untouched.
        assert user.pref_assets == ["BTC", "ETH"]
        assert user.pref_min_confidence == 8
        assert user.pref_paused is False
        assert user.is_active is True
        assert user.paper_trade is False


# ---------------------------------------------------------------------------
# /admin/sodex/symbols/refresh
# ---------------------------------------------------------------------------


class TestSymbolsRefresh:
    async def test_503_when_scheduler_disabled(self, admin_client):
        """No clients on app.state → scheduler is disabled → 503."""
        client, app = admin_client
        # Don't set sodex_spot_client / sodex_perps_client on app.state.
        resp = await client.post("/api/admin/sodex/symbols/refresh", headers=_HEADERS)
        assert resp.status_code == 503

    async def test_refresh_uses_state_clients(self, admin_client, db_session):
        """Attach mock clients to app.state; verify the route uses them
        and persists rows."""
        client, app = admin_client

        spot = AsyncMock(spec=SodexSpotClient)
        spot.get_symbols = AsyncMock(
            return_value=[SpotSymbol.model_construct(id=1, name="vBTC_vUSDC")]
        )
        perps = AsyncMock(spec=SodexPerpsClient)
        perps.get_symbols = AsyncMock(
            return_value=[PerpsSymbol.model_construct(id=2, name="vETH_vUSDC")]
        )
        app.state.sodex_spot_client = spot
        app.state.sodex_perps_client = perps

        resp = await client.post("/api/admin/sodex/symbols/refresh", headers=_HEADERS)
        assert resp.status_code == 200
        body = resp.json()
        assert body["spot_inserted"] == 1
        assert body["perps_inserted"] == 1

        rows = (await db_session.execute(select(SodexSymbol))).scalars().all()
        venues = {r.venue for r in rows}
        assert venues == {Venue.SODEX_SPOT.value, Venue.SODEX_PERPS.value}
