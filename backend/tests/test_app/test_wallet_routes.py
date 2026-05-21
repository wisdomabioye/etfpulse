"""PR D.4.2 — Wallet binding routes + SIWE verifier tests.

Pins:
  - /nonce happy path + 503 on missing domain config
  - /verify happy path (with siwe roundtrip)
  - /verify rejects: malformed message, domain mismatch, chain mismatch,
    bad signature, replayed nonce, unknown nonce, cross-bind attempt,
    bad signature shape (schema 422)
  - /verify creates User on first bind; finds existing on rebind
  - /me 401 unauth; 200 wallet bound + unbound
  - /api-key 401 unauth; 403 wallet unbound; 200 happy; spot + perps
  - Wallet upsert race (concurrent first-bind → one row)

Test fixtures synthesize SIWE messages + signatures via `eth_account`
locally. Production code never signs — the backend only verifies.
This is the same carve-out pattern as `scripts/sodex_verify/`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
import pytest
from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi import FastAPI
from siwe import SiweMessage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from etfpulse.api import auth_siwe as siwe_mod
from etfpulse.api.deps import get_db_session
from etfpulse.app import create_app
from etfpulse.config import settings
from etfpulse.models.user import User

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_siwe_state(monkeypatch):
    """Pin SIWE settings + flush nonce cache before each test.

    Every test gets a clean nonce store. `frontend_url` must be set
    so `siwe_domain` resolves to the expected fixture value.
    """
    monkeypatch.setattr(settings, "frontend_url", "https://etfpulse.example.com")
    monkeypatch.setattr(settings, "sodex_environment", "testnet")
    monkeypatch.setattr(settings, "wallet_nonce_ttl_seconds", 600)
    siwe_mod.reset_nonce_cache_for_tests()
    yield
    siwe_mod.reset_nonce_cache_for_tests()


@pytest.fixture
async def client(db_session: AsyncSession) -> AsyncIterator[httpx.AsyncClient]:
    """Real FastAPI app with the test DB session injected via override."""
    app: FastAPI = create_app()

    async def _override_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db_session] = _override_session
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as c:
        yield c


@pytest.fixture
def burner():
    """Fresh ephemeral keypair per test — never persisted, never sent
    over the wire. Used to produce signed SIWE messages locally to
    drive the verifier."""
    return Account.create()


def _build_signed_siwe(
    account,
    *,
    domain: str = "etfpulse.example.com",
    chain_id: int | None = None,
    nonce: str | None = None,
    statement: str | None = None,
    uri: str = "https://etfpulse.example.com",
) -> tuple[str, str]:
    """Construct + sign a SIWE message. Returns (message_text, signature_hex).

    If `nonce` is None, the test should have just requested one via
    `/api/wallet/nonce` and passed it in; we don't try to grab from
    the cache directly because the test wants to exercise the route.
    """
    if chain_id is None:
        chain_id = settings.sodex_chain_id
    if statement is None:
        statement = settings.siwe_statement
    if nonce is None:
        nonce = "fallback123abcdef"  # only for negative-path tests
    msg = SiweMessage(
        domain=domain,
        address=account.address,  # EIP-55 checksum
        statement=statement,
        uri=uri,
        version="1",
        chain_id=chain_id,
        nonce=nonce,
        issued_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
    )
    prepared = msg.prepare_message()
    signable = encode_defunct(text=prepared)
    sig = account.sign_message(signable).signature
    return prepared, "0x" + sig.hex()


async def _request_nonce(client: httpx.AsyncClient, address: str) -> dict:
    r = await client.post("/api/wallet/nonce", json={"address": address})
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# /nonce
# ---------------------------------------------------------------------------


async def test_nonce_happy_path(client, burner):
    body = await _request_nonce(client, burner.address)
    assert body["nonce"]  # non-empty
    assert body["domain"] == "etfpulse.example.com"
    assert body["chain_id"] == settings.sodex_chain_id
    assert body["statement"] == settings.siwe_statement
    assert body["version"] == "1"
    assert body["uri"] == "https://etfpulse.example.com"
    # issued_at + expires_at are ISO datetimes — Pydantic emits them as
    # serialized strings; we just check they parse.
    datetime.fromisoformat(body["issued_at"].replace("Z", "+00:00"))
    datetime.fromisoformat(body["expires_at"].replace("Z", "+00:00"))


async def test_nonce_rejects_malformed_address(client):
    r = await client.post("/api/wallet/nonce", json={"address": "not-an-address"})
    assert r.status_code == 422  # pydantic regex validator


async def test_nonce_503_when_no_frontend_url(client, monkeypatch):
    monkeypatch.setattr(settings, "frontend_url", "")
    r = await client.post("/api/wallet/nonce", json={"address": "0x" + "a" * 40})
    assert r.status_code == 503


async def test_nonce_two_requests_yield_distinct_nonces(client, burner):
    a = await _request_nonce(client, burner.address)
    b = await _request_nonce(client, burner.address)
    assert a["nonce"] != b["nonce"]


# ---------------------------------------------------------------------------
# /verify
# ---------------------------------------------------------------------------


async def test_verify_happy_path_creates_new_user(client, db_session, burner):
    nonce_body = await _request_nonce(client, burner.address)
    message, signature = _build_signed_siwe(burner, nonce=nonce_body["nonce"])

    r = await client.post(
        "/api/wallet/verify",
        json={"message": message, "signature": signature},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["jwt"]
    assert body["wallet_address"] == burner.address.lower()
    assert isinstance(body["user_id"], int)

    # User row was created with lowercased wallet
    result = await db_session.execute(
        select(User).where(User.wallet_address == burner.address.lower())
    )
    user = result.scalar_one()
    assert user.wallet_address == burner.address.lower()


async def test_verify_rebind_returns_existing_user(client, db_session, burner):
    # First bind creates the user
    nonce_body = await _request_nonce(client, burner.address)
    message, signature = _build_signed_siwe(burner, nonce=nonce_body["nonce"])
    r1 = await client.post(
        "/api/wallet/verify",
        json={"message": message, "signature": signature},
    )
    first_uid = r1.json()["user_id"]

    # Re-bind with a fresh nonce returns the SAME user_id
    nonce_body2 = await _request_nonce(client, burner.address)
    msg2, sig2 = _build_signed_siwe(burner, nonce=nonce_body2["nonce"])
    r2 = await client.post(
        "/api/wallet/verify",
        json={"message": msg2, "signature": sig2},
    )
    assert r2.status_code == 200
    assert r2.json()["user_id"] == first_uid


async def test_verify_rejects_unknown_nonce(client, burner):
    """Nonce never issued — direct attempt to verify with a fabricated nonce."""
    message, signature = _build_signed_siwe(burner, nonce="fabricated1234567")
    r = await client.post(
        "/api/wallet/verify",
        json={"message": message, "signature": signature},
    )
    assert r.status_code == 400
    assert "nonce" in r.json()["detail"].lower()


async def test_verify_rejects_replayed_nonce(client, burner):
    """Same message+signature submitted twice — second fails (single-use)."""
    nonce_body = await _request_nonce(client, burner.address)
    message, signature = _build_signed_siwe(burner, nonce=nonce_body["nonce"])
    r1 = await client.post(
        "/api/wallet/verify",
        json={"message": message, "signature": signature},
    )
    assert r1.status_code == 200
    r2 = await client.post(
        "/api/wallet/verify",
        json={"message": message, "signature": signature},
    )
    assert r2.status_code == 400


async def test_verify_rejects_domain_mismatch(client, burner):
    nonce_body = await _request_nonce(client, burner.address)
    message, signature = _build_signed_siwe(
        burner, nonce=nonce_body["nonce"], domain="phisher.example"
    )
    r = await client.post(
        "/api/wallet/verify",
        json={"message": message, "signature": signature},
    )
    assert r.status_code == 400
    assert "domain" in r.json()["detail"]


async def test_verify_rejects_chain_mismatch(client, burner):
    nonce_body = await _request_nonce(client, burner.address)
    # Mainnet chainId on a testnet-configured deployment
    message, signature = _build_signed_siwe(burner, nonce=nonce_body["nonce"], chain_id=1)
    r = await client.post(
        "/api/wallet/verify",
        json={"message": message, "signature": signature},
    )
    assert r.status_code == 400
    assert "chain" in r.json()["detail"]


async def test_verify_rejects_cross_binding(client, burner):
    """Nonce issued for address A; submit a message signed by address B
    (with the right nonce embedded in B's message). Must reject —
    otherwise an attacker could request a nonce for someone else's
    address and slip in their own signed message."""
    other = Account.create()
    nonce_body = await _request_nonce(client, burner.address)
    # `other` signs a message with burner's nonce
    message, signature = _build_signed_siwe(other, nonce=nonce_body["nonce"])
    r = await client.post(
        "/api/wallet/verify",
        json={"message": message, "signature": signature},
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "address mismatch"


async def test_verify_rejects_tampered_signature(client, burner):
    """Valid nonce + matching address, but signature has one byte
    flipped inside the `r` component. `siwe.verify` recovers a
    different address; we reject.

    NOTE: do NOT tamper the trailing `v` byte alone — eth_account
    normalises `v ∈ {0, 1, 27, 28}` to the same `recovery_id ∈ {0, 1}`,
    so flipping `0x1b` → `0x00` keeps `recovery_id=0` and recovers the
    same address. Picking a hex pair inside `r` (chars 2..66 of the
    0x-prefixed signature) guarantees the math no longer corresponds
    to a signature over our message.
    """
    nonce_body = await _request_nonce(client, burner.address)
    message, signature = _build_signed_siwe(burner, nonce=nonce_body["nonce"])
    # signature shape: "0x" + r(64) + s(64) + v(2). Flip 2 chars at
    # offset 10 (well inside r, deterministic across runs).
    pre, mid, post = signature[:10], signature[10:12], signature[12:]
    flipped = "ff" if mid != "ff" else "00"
    tampered = pre + flipped + post
    r = await client.post(
        "/api/wallet/verify",
        json={"message": message, "signature": tampered},
    )
    assert r.status_code == 400


async def test_verify_rejects_zero_signature_via_catchall(client, burner):
    """All-zero r/s/v signature — passes pydantic regex (130 hex chars)
    but fails ECDSA recovery at the eth_keys layer with `BadSignature`,
    which does NOT inherit from `siwe.VerificationError`. Pins the
    `except Exception` catch-all branch (the route would 500 without
    it — see review-pass log around D.4.3)."""
    nonce_body = await _request_nonce(client, burner.address)
    message, _ = _build_signed_siwe(burner, nonce=nonce_body["nonce"])
    # Cryptographically invalid: r=0 violates the secp256k1 curve
    # constraint that 0 < r < n. The siwe lib delegates recovery to
    # eth_account/eth_keys which raise on this before any address
    # comparison happens.
    zero_signature = "0x" + "00" * 32 + "00" * 32 + "1b"
    r = await client.post(
        "/api/wallet/verify",
        json={"message": message, "signature": zero_signature},
    )
    assert r.status_code == 400, r.text
    # Detail string should match the catch-all branch.
    assert r.json()["detail"] == "invalid signature"


async def test_verify_rejects_malformed_signature_shape(client, burner):
    """Pydantic regex rejects junk before the route runs."""
    nonce_body = await _request_nonce(client, burner.address)
    message, _ = _build_signed_siwe(burner, nonce=nonce_body["nonce"])
    r = await client.post(
        "/api/wallet/verify",
        json={"message": message, "signature": "0xabc"},
    )
    assert r.status_code == 422


async def test_verify_rejects_malformed_message(client):
    r = await client.post(
        "/api/wallet/verify",
        json={"message": "not a siwe message", "signature": "0x" + "a" * 130},
    )
    assert r.status_code == 400
    assert "malformed" in r.json()["detail"]


# ---------------------------------------------------------------------------
# /me
# ---------------------------------------------------------------------------


async def test_me_401_unauth(client):
    r = await client.get("/api/wallet/me")
    assert r.status_code == 401


async def test_me_200_wallet_bound(client, db_session, burner):
    nonce_body = await _request_nonce(client, burner.address)
    message, signature = _build_signed_siwe(burner, nonce=nonce_body["nonce"])
    bind = await client.post(
        "/api/wallet/verify",
        json={"message": message, "signature": signature},
    )
    token = bind.json()["jwt"]
    r = await client.get(
        "/api/wallet/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["wallet_address"] == burner.address.lower()
    assert body["paper_trade"] is False
    assert body["sodex_account_id"] is None
    assert body["sodex_spot_api_key_name"] is None
    assert body["sodex_perps_api_key_name"] is None


async def test_me_works_for_wallet_unbound_user(client, db_session):
    """User has a JWT but no wallet (D.5 future Telegram path). `/me`
    uses `get_current_user_unbound` so it still returns 200."""
    from etfpulse.api.auth import mint_jwt

    u = User(wallet_address=None)
    db_session.add(u)
    await db_session.flush()
    token = mint_jwt(u.id)
    r = await client.get(
        "/api/wallet/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    assert r.json()["wallet_address"] is None


# ---------------------------------------------------------------------------
# /api-key
# ---------------------------------------------------------------------------


async def test_api_key_401_unauth(client):
    r = await client.post(
        "/api/wallet/api-key",
        json={"venue": "sodex_spot", "api_key_name": "default", "sodex_account_id": 1},
    )
    assert r.status_code == 401


async def test_api_key_403_wallet_unbound(client, db_session):
    from etfpulse.api.auth import mint_jwt

    u = User(wallet_address=None)
    db_session.add(u)
    await db_session.flush()
    token = mint_jwt(u.id)
    r = await client.post(
        "/api/wallet/api-key",
        json={"venue": "sodex_spot", "api_key_name": "default", "sodex_account_id": 1},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 403


async def test_api_key_sets_spot_then_perps(client, db_session, burner):
    nonce_body = await _request_nonce(client, burner.address)
    message, signature = _build_signed_siwe(burner, nonce=nonce_body["nonce"])
    bind = await client.post(
        "/api/wallet/verify",
        json={"message": message, "signature": signature},
    )
    token = bind.json()["jwt"]
    h = {"Authorization": f"Bearer {token}"}

    r1 = await client.post(
        "/api/wallet/api-key",
        json={"venue": "sodex_spot", "api_key_name": "default-spot", "sodex_account_id": 42},
        headers=h,
    )
    assert r1.status_code == 200, r1.text
    assert r1.json()["api_key_name"] == "default-spot"

    r2 = await client.post(
        "/api/wallet/api-key",
        json={"venue": "sodex_perps", "api_key_name": "default-perps", "sodex_account_id": 42},
        headers=h,
    )
    assert r2.status_code == 200

    # Inspect via /me
    me = await client.get("/api/wallet/me", headers=h)
    body = me.json()
    assert body["sodex_spot_api_key_name"] == "default-spot"
    assert body["sodex_perps_api_key_name"] == "default-perps"
    assert body["sodex_account_id"] == 42


async def test_api_key_rejects_bad_venue(client, db_session, burner):
    """Schema validator rejects unknown venue strings (422 before route)."""
    nonce_body = await _request_nonce(client, burner.address)
    message, signature = _build_signed_siwe(burner, nonce=nonce_body["nonce"])
    bind = await client.post(
        "/api/wallet/verify",
        json={"message": message, "signature": signature},
    )
    token = bind.json()["jwt"]
    r = await client.post(
        "/api/wallet/api-key",
        json={"venue": "binance", "api_key_name": "x", "sodex_account_id": 1},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 422


async def test_api_key_rejects_malformed_name(client, db_session, burner):
    nonce_body = await _request_nonce(client, burner.address)
    message, signature = _build_signed_siwe(burner, nonce=nonce_body["nonce"])
    bind = await client.post(
        "/api/wallet/verify",
        json={"message": message, "signature": signature},
    )
    token = bind.json()["jwt"]
    r = await client.post(
        "/api/wallet/api-key",
        json={"venue": "sodex_spot", "api_key_name": "spaces not allowed", "sodex_account_id": 1},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Wallet upsert collision: not race-tested (single asyncio loop per
# test) but the IntegrityError handler is exercised by directly
# inserting a conflicting row.
# ---------------------------------------------------------------------------


async def test_verify_with_authed_jwt_binds_to_existing_user(client, db_session, burner):
    """PR D.5 Option A: a Telegram-WebApp-bound user (JWT present,
    wallet_address NULL) running SIWE must bind the wallet to THAT
    existing user, NOT create a new one. Otherwise the user has two
    rows (one keyed by tg_user_id, one keyed by wallet_address) and
    the FE silently swaps to the wallet-keyed JWT."""
    from etfpulse.api.auth import mint_jwt

    # Pre-create a wallet-less user (simulating Telegram-WebApp bind).
    existing = User(wallet_address=None)
    db_session.add(existing)
    await db_session.flush()
    existing_id = existing.id
    jwt = mint_jwt(existing_id)

    # SIWE bind WITH the JWT attached.
    nonce_body = await _request_nonce(client, burner.address)
    message, signature = _build_signed_siwe(burner, nonce=nonce_body["nonce"])
    r = await client.post(
        "/api/wallet/verify",
        json={"message": message, "signature": signature},
        headers={"Authorization": f"Bearer {jwt}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # MUST be the existing user_id, not a fresh insert.
    assert body["user_id"] == existing_id
    assert body["wallet_address"] == burner.address.lower()

    # No duplicate user row exists with this wallet.
    result = await db_session.execute(
        select(User).where(User.wallet_address == burner.address.lower())
    )
    rows = result.scalars().all()
    assert len(rows) == 1
    assert rows[0].id == existing_id


async def test_verify_authed_path_idempotent_rebind(client, db_session, burner):
    """Re-running SIWE with the SAME wallet for an already-bound user
    is a no-op — same user_id, same wallet. Useful when the FE
    retries verify after a network blip."""
    from etfpulse.api.auth import mint_jwt

    user = User(wallet_address=burner.address.lower())
    db_session.add(user)
    await db_session.flush()
    user_id = user.id
    jwt = mint_jwt(user_id)

    nonce_body = await _request_nonce(client, burner.address)
    message, signature = _build_signed_siwe(burner, nonce=nonce_body["nonce"])
    r = await client.post(
        "/api/wallet/verify",
        json={"message": message, "signature": signature},
        headers={"Authorization": f"Bearer {jwt}"},
    )
    assert r.status_code == 200
    assert r.json()["user_id"] == user_id


async def test_verify_authed_user_with_different_wallet_409(client, db_session, burner):
    """User already has a different wallet → 409 (no silent swap).
    Operator must explicitly unbind the prior wallet first."""
    from etfpulse.api.auth import mint_jwt

    user = User(wallet_address="0x" + "1" * 40)  # different wallet
    db_session.add(user)
    await db_session.flush()
    user_id = user.id
    jwt = mint_jwt(user_id)

    nonce_body = await _request_nonce(client, burner.address)
    message, signature = _build_signed_siwe(burner, nonce=nonce_body["nonce"])
    r = await client.post(
        "/api/wallet/verify",
        json={"message": message, "signature": signature},
        headers={"Authorization": f"Bearer {jwt}"},
    )
    assert r.status_code == 409
    assert r.json()["detail"] == "wallet_swap_not_allowed"


async def test_verify_authed_wallet_already_owned_by_other_user_409(client, db_session, burner):
    """Wallet is already bound to user A; user B tries to bind it.
    409. The partial UNIQUE index would also catch this at flush, but
    the pre-check produces a friendlier 409 with a specific detail."""
    from etfpulse.api.auth import mint_jwt

    user_a = User(wallet_address=burner.address.lower())  # owns the wallet
    user_b = User(wallet_address=None)  # tries to claim it
    db_session.add_all([user_a, user_b])
    await db_session.flush()
    user_b_id = user_b.id
    jwt = mint_jwt(user_b_id)

    nonce_body = await _request_nonce(client, burner.address)
    message, signature = _build_signed_siwe(burner, nonce=nonce_body["nonce"])
    r = await client.post(
        "/api/wallet/verify",
        json={"message": message, "signature": signature},
        headers={"Authorization": f"Bearer {jwt}"},
    )
    assert r.status_code == 409
    assert r.json()["detail"] == "wallet_already_bound_to_other_user"


async def test_verify_stale_jwt_falls_through_to_anonymous_path(client, db_session, burner):
    """A broken-but-non-empty Authorization header (e.g., expired
    JWT, garbled, wrong audience) MUST NOT block a legitimate
    first-time SIWE bind. The route silently falls through to the
    anonymous create-or-find path."""
    nonce_body = await _request_nonce(client, burner.address)
    message, signature = _build_signed_siwe(burner, nonce=nonce_body["nonce"])
    r = await client.post(
        "/api/wallet/verify",
        json={"message": message, "signature": signature},
        headers={"Authorization": "Bearer not.a.valid.jwt"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["wallet_address"] == burner.address.lower()


async def test_verify_handles_concurrent_insert_race(client, db_session, burner):
    """Simulate the race: another transaction inserts the same wallet
    before our flush. The route's IntegrityError handler rolls back +
    re-selects, returning the pre-existing user."""
    # Pre-create a user with this wallet address so the route's flush
    # collides on the partial UNIQUE index.
    existing = User(wallet_address=burner.address.lower())
    db_session.add(existing)
    await db_session.flush()
    existing_id = existing.id

    nonce_body = await _request_nonce(client, burner.address)
    message, signature = _build_signed_siwe(burner, nonce=nonce_body["nonce"])
    r = await client.post(
        "/api/wallet/verify",
        json={"message": message, "signature": signature},
    )
    assert r.status_code == 200
    assert r.json()["user_id"] == existing_id
