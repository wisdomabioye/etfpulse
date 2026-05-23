"""JWT auth foundation tests (PR D.4.1).

Pins the security-critical invariants:
  - mint→verify roundtrip
  - expired token rejected
  - wrong-audience token rejected
  - alg-confusion (`alg=none`) rejected
  - signature mismatch rejected
  - missing required claims rejected
  - `get_current_user` 401 on missing/malformed header
  - `get_current_user` 403 on wallet-unbound user (D.4 execution gate)
  - `get_current_user` 401 on vanished user (mint→delete race)
  - `get_current_user_unbound` permits wallet-unbound users
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import jwt
import pytest
from fastapi import APIRouter, Depends, FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from etfpulse.api import auth as auth_mod
from etfpulse.api.auth import (
    JWTError,
    get_current_user,
    get_current_user_unbound,
    mint_jwt,
    verify_jwt,
)
from etfpulse.api.deps import get_db_session
from etfpulse.config import settings
from etfpulse.models.user import User

# ---------------------------------------------------------------------------
# Pure mint/verify
# ---------------------------------------------------------------------------


def test_mint_verify_roundtrip():
    token = mint_jwt(42)
    claims = verify_jwt(token)
    assert claims["user_id"] == 42
    assert claims["sub"] == "42"
    assert claims["aud"] == "execution"
    assert "iat" in claims and "exp" in claims and "jti" in claims


def test_mint_rejects_non_positive_user_id():
    """`user_id <= 0` is a programmer bug — `session.get(User, 0)` would
    always return None and the user gets a 401 forever. Fail loud at
    the mint site so the bug surfaces at the moment it occurs."""
    with pytest.raises(ValueError, match="user_id must be > 0"):
        mint_jwt(0)
    with pytest.raises(ValueError, match="user_id must be > 0"):
        mint_jwt(-1)


def test_mint_emits_string_sub_per_spec():
    """RFC 7519 §4.1.2 — `sub` MUST be a string. pyjwt allows int but
    third-party verifiers may reject it; we always emit string."""
    token = mint_jwt(99)
    raw = jwt.decode(
        token,
        auth_mod._resolve_secret(),
        algorithms=["HS256"],
        audience="execution",
        options={"verify_signature": True},
    )
    assert isinstance(raw["sub"], str)
    assert raw["sub"] == "99"


def test_mint_default_ttl_uses_settings_jwt_ttl_seconds(monkeypatch):
    """#78.9 — Without `ttl_seconds`, exp - iat MUST equal
    `settings.jwt_ttl_seconds`. Pin so a future refactor can't silently
    swap default TTLs."""
    from etfpulse.config import settings

    monkeypatch.setattr(settings, "jwt_ttl_seconds", 12345)
    token = mint_jwt(7)
    claims = verify_jwt(token)
    assert claims["exp"] - claims["iat"] == 12345


def test_mint_ttl_override_takes_precedence(monkeypatch):
    """#78.9 — `ttl_seconds=N` overrides `settings.jwt_ttl_seconds`.
    Verifies the WebApp path's tighter-TTL behaviour: setting `jwt_ttl_seconds`
    to one value and passing a different `ttl_seconds` MUST use the override,
    not the setting."""
    from etfpulse.config import settings

    monkeypatch.setattr(settings, "jwt_ttl_seconds", 86400)  # 24h
    token = mint_jwt(7, ttl_seconds=3600)  # 1h override
    claims = verify_jwt(token)
    assert claims["exp"] - claims["iat"] == 3600


def test_mint_rejects_zero_or_negative_ttl():
    """#78.9 — A zero or negative `ttl_seconds` would mint a token
    already expired at iat; the caller has no recovery path. Fail loud
    at the mint site, same as the user_id<=0 contract."""
    with pytest.raises(ValueError, match="ttl_seconds must be > 0"):
        mint_jwt(7, ttl_seconds=0)
    with pytest.raises(ValueError, match="ttl_seconds must be > 0"):
        mint_jwt(7, ttl_seconds=-1)


def test_mint_ttl_override_none_falls_through_to_default(monkeypatch):
    """`ttl_seconds=None` (the default) MUST behave identically to
    omitting the kwarg. Catches a regression where None gets validated
    against the > 0 check."""
    from etfpulse.config import settings

    monkeypatch.setattr(settings, "jwt_ttl_seconds", 999)
    token = mint_jwt(7, ttl_seconds=None)
    claims = verify_jwt(token)
    assert claims["exp"] - claims["iat"] == 999


def test_verify_rejects_expired_token():
    # Mint with a stale exp by directly building the payload.
    now = datetime.now(UTC)
    payload = {
        "sub": "7",
        "aud": "execution",
        "iat": int((now - timedelta(hours=2)).timestamp()),
        "exp": int((now - timedelta(hours=1)).timestamp()),
        "jti": "stale",
    }
    token = jwt.encode(payload, auth_mod._resolve_secret(), algorithm="HS256")
    with pytest.raises(JWTError) as exc:
        verify_jwt(token)
    assert "expired" in exc.value.detail


def test_verify_rejects_wrong_audience():
    now = datetime.now(UTC)
    payload = {
        "sub": "7",
        "aud": "analytics",  # not 'execution'
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=1)).timestamp()),
        "jti": "x",
    }
    token = jwt.encode(payload, auth_mod._resolve_secret(), algorithm="HS256")
    with pytest.raises(JWTError):
        verify_jwt(token, audience="execution")


def test_verify_rejects_alg_none_attack():
    """The classic alg-confusion vulnerability — a token signed with
    `alg=none` is presented as if authentic. `pyjwt` accepts it ONLY if
    the verifier explicitly allows `none` in `algorithms`. We pin HS256
    so this token is rejected with `InvalidAlgorithmError`."""
    now = datetime.now(UTC)
    payload = {
        "sub": "7",
        "aud": "execution",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=1)).timestamp()),
        "jti": "x",
    }
    # `algorithm='none'` requires `key=''` in pyjwt 2.x — that's the
    # exact shape an attacker would craft.
    token = jwt.encode(payload, "", algorithm="none")
    with pytest.raises(JWTError):
        verify_jwt(token)


def test_verify_rejects_wrong_signature():
    now = datetime.now(UTC)
    payload = {
        "sub": "7",
        "aud": "execution",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=1)).timestamp()),
        "jti": "x",
    }
    bogus = jwt.encode(payload, "not-the-real-secret", algorithm="HS256")
    with pytest.raises(JWTError):
        verify_jwt(bogus)


def test_verify_rejects_malformed_token():
    with pytest.raises(JWTError):
        verify_jwt("not.a.jwt")


def test_verify_rejects_missing_claims():
    """`options={'require': ['sub','aud','iat','exp']}` makes missing
    claims an error rather than silently None."""
    # Build a token missing `exp` — pyjwt accepts the encode, but
    # decode with `require=['exp']` rejects it.
    token = jwt.encode(
        {"sub": "5", "aud": "execution"},
        auth_mod._resolve_secret(),
        algorithm="HS256",
    )
    with pytest.raises(JWTError):
        verify_jwt(token)


def test_verify_rejects_token_without_aud():
    """Token with no `aud` claim at all. pyjwt raises
    `MissingRequiredClaimError` (subclass of PyJWTError) — falls
    through to our catch-all 401. Distinct test from
    `test_verify_rejects_missing_claims` (which drops `exp`)."""
    now = datetime.now(UTC)
    payload = {
        "sub": "7",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=1)).timestamp()),
        "jti": "x",
    }  # no `aud`
    token = jwt.encode(payload, auth_mod._resolve_secret(), algorithm="HS256")
    with pytest.raises(JWTError):
        verify_jwt(token)


def test_verify_rejects_non_numeric_sub():
    now = datetime.now(UTC)
    payload = {
        "sub": "not-a-number",
        "aud": "execution",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=1)).timestamp()),
        "jti": "x",
    }
    token = jwt.encode(payload, auth_mod._resolve_secret(), algorithm="HS256")
    with pytest.raises(JWTError):
        verify_jwt(token)


def test_resolve_secret_uses_settings_when_set(monkeypatch):
    monkeypatch.setattr(settings, "jwt_secret", "configured-secret")
    # Bypass the ephemeral cache so we see the new value.
    monkeypatch.setattr(auth_mod, "_ephemeral_secret", None)
    assert auth_mod._resolve_secret() == "configured-secret"


def test_resolve_secret_generates_ephemeral_on_empty(monkeypatch):
    monkeypatch.setattr(settings, "jwt_secret", "")
    monkeypatch.setattr(auth_mod, "_ephemeral_secret", None)
    s1 = auth_mod._resolve_secret()
    s2 = auth_mod._resolve_secret()
    assert s1 and s1 == s2  # cached, same across calls
    assert len(s1) >= 32


# ---------------------------------------------------------------------------
# get_current_user (FastAPI dep — exercised via a tiny test app)
# ---------------------------------------------------------------------------


def _build_test_app(db_session: AsyncSession) -> FastAPI:
    """Mount two probe routes on a fresh FastAPI — one requiring a bound
    wallet, one not. Override get_db_session to inject the test session."""
    app = FastAPI()
    router = APIRouter()

    @router.get("/_probe/bound")
    async def bound(user: User = Depends(get_current_user)):
        return {"user_id": user.id, "wallet": user.wallet_address}

    @router.get("/_probe/unbound")
    async def unbound(user: User = Depends(get_current_user_unbound)):
        return {"user_id": user.id, "wallet": user.wallet_address}

    app.include_router(router)

    async def _override_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db_session] = _override_session
    return app


@pytest.fixture
async def bound_user(db_session: AsyncSession) -> User:
    u = User(wallet_address="0x" + "a" * 40)
    db_session.add(u)
    await db_session.flush()
    return u


@pytest.fixture
async def unbound_user(db_session: AsyncSession) -> User:
    u = User(wallet_address=None)
    db_session.add(u)
    await db_session.flush()
    return u


async def test_get_current_user_happy_path(db_session, bound_user):
    app = _build_test_app(db_session)
    token = mint_jwt(bound_user.id)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/_probe/bound", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json()["user_id"] == bound_user.id


async def test_get_current_user_401_on_missing_header(db_session, bound_user):
    app = _build_test_app(db_session)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/_probe/bound")
    assert r.status_code == 401


async def test_get_current_user_401_on_non_bearer_scheme(db_session, bound_user):
    app = _build_test_app(db_session)
    token = mint_jwt(bound_user.id)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/_probe/bound", headers={"Authorization": f"Basic {token}"})
    assert r.status_code == 401


async def test_get_current_user_accepts_case_insensitive_bearer(db_session, bound_user):
    """RFC 7235 §2.1 / RFC 6750 §2.1 — auth scheme is case-insensitive.
    A strict `startswith('Bearer ')` check would reject valid clients."""
    app = _build_test_app(db_session)
    token = mint_jwt(bound_user.id)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        # lowercase
        r1 = await c.get("/_probe/bound", headers={"Authorization": f"bearer {token}"})
        # mixed-case
        r2 = await c.get("/_probe/bound", headers={"Authorization": f"BeArEr {token}"})
        # uppercase
        r3 = await c.get("/_probe/bound", headers={"Authorization": f"BEARER {token}"})
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r3.status_code == 200


async def test_get_current_user_401_on_bearer_no_token(db_session, bound_user):
    """`Bearer` alone (no value) — `split(None, 1)` returns single
    element; len != 2 → 401."""
    app = _build_test_app(db_session)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/_probe/bound", headers={"Authorization": "Bearer"})
    assert r.status_code == 401


async def test_get_current_user_401_on_empty_bearer(db_session, bound_user):
    app = _build_test_app(db_session)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/_probe/bound", headers={"Authorization": "Bearer "})
    assert r.status_code == 401


async def test_get_current_user_401_on_invalid_token(db_session, bound_user):
    app = _build_test_app(db_session)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/_probe/bound", headers={"Authorization": "Bearer not.a.jwt"})
    assert r.status_code == 401


async def test_get_current_user_403_on_wallet_unbound(db_session, unbound_user):
    app = _build_test_app(db_session)
    token = mint_jwt(unbound_user.id)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/_probe/bound", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403
    assert r.json()["detail"] == "wallet_not_bound"


async def test_get_current_user_unbound_admits_wallet_null(db_session, unbound_user):
    app = _build_test_app(db_session)
    token = mint_jwt(unbound_user.id)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/_probe/unbound", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json()["wallet"] is None


async def test_get_current_user_401_on_vanished_user(db_session, bound_user):
    """Mint a token, delete the user, then call — token verifies but
    DB lookup returns None. Must 401 (not 500, not 404)."""
    app = _build_test_app(db_session)
    token = mint_jwt(bound_user.id)
    await db_session.delete(bound_user)
    await db_session.flush()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/_probe/bound", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401
