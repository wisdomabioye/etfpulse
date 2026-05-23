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


async def test_verify_applies_paper_trade_default_true(client, db_session, burner, monkeypatch):
    """PR #184 — SIWE-bound new users inherit `settings.user_paper_trade_default`.
    Default True is the safe-by-default posture for mainnet. Regression
    that drops the `paper_trade=` kwarg from `_resolve_or_create_user_by_wallet`
    would silently expose new mainnet users to live execution before
    operator review."""
    from etfpulse.config import settings

    monkeypatch.setattr(settings, "user_paper_trade_default", True)
    nonce_body = await _request_nonce(client, burner.address)
    message, signature = _build_signed_siwe(burner, nonce=nonce_body["nonce"])
    r = await client.post(
        "/api/wallet/verify",
        json={"message": message, "signature": signature},
    )
    assert r.status_code == 200

    user_id = r.json()["user_id"]
    user = await db_session.get(User, user_id)
    assert user is not None
    assert user.paper_trade is True


async def test_verify_applies_paper_trade_default_false_when_overridden(
    client, db_session, burner, monkeypatch
):
    """Counterpart: SIWE-bound users inherit False when the setting is
    flipped. Confirms the default is config-driven, not hardcoded."""
    from etfpulse.config import settings

    monkeypatch.setattr(settings, "user_paper_trade_default", False)
    nonce_body = await _request_nonce(client, burner.address)
    message, signature = _build_signed_siwe(burner, nonce=nonce_body["nonce"])
    r = await client.post(
        "/api/wallet/verify",
        json={"message": message, "signature": signature},
    )
    assert r.status_code == 200

    user_id = r.json()["user_id"]
    user = await db_session.get(User, user_id)
    assert user is not None
    assert user.paper_trade is False


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
    # PR #184 — freshly-bound user defaults to paper_trade=True (safe-
    # by-default). Operator opts INTO live execution via the admin
    # paper-trade route. Was False pre-#184.
    assert body["paper_trade"] is True
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
# POST /api/wallet/request-live (#185)
# ---------------------------------------------------------------------------


async def _bind_and_get_token(client, burner) -> tuple[str, int]:
    """Helper: run a full SIWE bind, return (jwt, user_id)."""
    nonce_body = await _request_nonce(client, burner.address)
    message, signature = _build_signed_siwe(burner, nonce=nonce_body["nonce"])
    r = await client.post(
        "/api/wallet/verify",
        json={"message": message, "signature": signature},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    return body["jwt"], body["user_id"]


@pytest.fixture(autouse=True)
def _clear_request_live_cooldown():
    """The route uses an in-memory TTLCache for per-user cooldown.
    Tests that hit the route would carry cooldown state across cases
    (within one process) without this. Clear before AND after to
    isolate."""
    from etfpulse.api.routes.wallet import _REQUEST_LIVE_COOLDOWN

    _REQUEST_LIVE_COOLDOWN.clear()
    yield
    _REQUEST_LIVE_COOLDOWN.clear()


@pytest.fixture
def _bot_enabled_with_operator_chat(monkeypatch):
    """Boots a fully-configured bot + operator chat for request-live
    tests. The 4-field Telegram config + non-zero operator chat id are
    both required by the route — independent monkeypatches keep each
    test focused on which gate it's exercising."""
    monkeypatch.setattr(settings, "run_bot", True)
    monkeypatch.setattr(settings, "telegram_bot_token", "1234:fake-token")
    monkeypatch.setattr(settings, "telegram_public_url", "https://etfpulse.example.com")
    monkeypatch.setattr(settings, "telegram_webhook_secret", "x" * 32)
    monkeypatch.setattr(settings, "telegram_webhook_url_suffix", "test-suffix")
    monkeypatch.setattr(settings, "operator_telegram_chat_id", -1001234567890)


async def test_request_live_401_unauth(client):
    r = await client.post("/api/wallet/request-live", json={})
    assert r.status_code == 401


async def test_request_live_403_wallet_unbound(client, db_session, monkeypatch):
    """Wallet-less Telegram user (Option A path mid-bind) — `get_current_user`
    enforces wallet presence so the route 403s before any bot interaction."""
    from etfpulse.api.auth import mint_jwt

    u = User(wallet_address=None)
    db_session.add(u)
    await db_session.flush()
    token = mint_jwt(u.id)
    r = await client.post(
        "/api/wallet/request-live",
        json={},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 403


async def test_request_live_503_when_bot_disabled(
    client, db_session, burner, monkeypatch
):
    """All four telegram fields empty → bot disabled → 503 with a clear
    detail string (NOT 404 — this is a user-facing affordance, info-leak
    policy doesn't apply)."""
    monkeypatch.setattr(settings, "run_bot", False)
    token, _ = await _bind_and_get_token(client, burner)

    r = await client.post(
        "/api/wallet/request-live",
        json={},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 503
    assert "bot not configured" in r.json()["detail"]


async def test_request_live_503_when_operator_chat_unset(
    client, db_session, burner, monkeypatch
):
    """Bot enabled but operator chat id is 0 → 503 with distinct detail
    so operators see which knob is missing."""
    monkeypatch.setattr(settings, "run_bot", True)
    monkeypatch.setattr(settings, "telegram_bot_token", "1234:fake-token")
    monkeypatch.setattr(settings, "telegram_public_url", "https://etfpulse.example.com")
    monkeypatch.setattr(settings, "telegram_webhook_secret", "x" * 32)
    monkeypatch.setattr(settings, "telegram_webhook_url_suffix", "test-suffix")
    monkeypatch.setattr(settings, "operator_telegram_chat_id", 0)
    token, _ = await _bind_and_get_token(client, burner)

    r = await client.post(
        "/api/wallet/request-live",
        json={},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 503
    assert "operator contact channel not configured" in r.json()["detail"]


async def test_request_live_happy_path_sends_telegram_message(
    client, db_session, burner, monkeypatch, _bot_enabled_with_operator_chat
):
    """Happy path — request goes through, operator gets a Telegram message
    with the user's id + wallet + note. The route does NOT flip paper_trade."""
    from unittest.mock import AsyncMock

    from etfpulse.adapters import telegram as telegram_mod

    sent: list[dict] = []

    async def fake_send(*, chat_id, text, parse_mode, reply_markup=None):
        sent.append({"chat_id": chat_id, "text": text, "parse_mode": parse_mode})

    monkeypatch.setattr(telegram_mod.telegram_client, "send_message", AsyncMock(side_effect=fake_send))

    token, user_id = await _bind_and_get_token(client, burner)
    r = await client.post(
        "/api/wallet/request-live",
        json={"note": "First paper-trade run was clean."},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert "paper-trade" in body["message"]

    # Exactly one Telegram send.
    assert len(sent) == 1
    msg = sent[0]
    assert msg["chat_id"] == -1001234567890
    assert msg["parse_mode"] == "HTML"
    assert f"<code>{user_id}</code>" in msg["text"]
    assert burner.address.lower() in msg["text"].lower()
    assert "First paper-trade run was clean." in msg["text"]

    # Route did NOT flip paper_trade — operator stays the gatekeeper.
    user = await db_session.get(User, user_id)
    assert user is not None
    # paper_trade is True per #184 default; the request-live route MUST
    # leave it that way.
    assert user.paper_trade is True


async def test_request_live_html_escapes_note(
    client, db_session, burner, monkeypatch, _bot_enabled_with_operator_chat
):
    """Hostile note must be HTML-escaped before embedding in the Telegram
    message (we render parse_mode=HTML). Pin this so a future format
    change can't introduce injection."""
    from unittest.mock import AsyncMock

    from etfpulse.adapters import telegram as telegram_mod

    sent: list[dict] = []

    async def fake_send(*, chat_id, text, parse_mode, reply_markup=None):
        sent.append({"text": text})

    monkeypatch.setattr(telegram_mod.telegram_client, "send_message", AsyncMock(side_effect=fake_send))

    token, _ = await _bind_and_get_token(client, burner)
    r = await client.post(
        "/api/wallet/request-live",
        json={"note": "<script>alert('xss')</script>"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200

    text = sent[0]["text"]
    # The literal `<script>` MUST NOT appear; HTML-escaped form MUST.
    assert "<script>" not in text
    assert "&lt;script&gt;" in text


async def test_request_live_cooldown_blocks_repeat(
    client, db_session, burner, monkeypatch, _bot_enabled_with_operator_chat
):
    """Per-user cooldown — second request within the window returns
    429 with a time-remaining hint."""
    from unittest.mock import AsyncMock

    from etfpulse.adapters import telegram as telegram_mod

    monkeypatch.setattr(telegram_mod.telegram_client, "send_message", AsyncMock())

    token, _ = await _bind_and_get_token(client, burner)

    r1 = await client.post(
        "/api/wallet/request-live",
        json={},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r1.status_code == 200

    r2 = await client.post(
        "/api/wallet/request-live",
        json={},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r2.status_code == 429
    assert "cooldown" in r2.json()["detail"]


async def test_request_live_503_when_telegram_send_fails(
    client, db_session, burner, monkeypatch, _bot_enabled_with_operator_chat
):
    """Telegram raise → 503 with user-friendly message; cooldown NOT set
    (so user can retry after fixing whatever transient issue)."""
    from unittest.mock import AsyncMock

    from etfpulse.adapters import telegram as telegram_mod
    from etfpulse.adapters.telegram import TelegramError
    from etfpulse.api.routes.wallet import _REQUEST_LIVE_COOLDOWN

    monkeypatch.setattr(
        telegram_mod.telegram_client,
        "send_message",
        AsyncMock(side_effect=TelegramError("upstream rate limited")),
    )

    token, user_id = await _bind_and_get_token(client, burner)
    r = await client.post(
        "/api/wallet/request-live",
        json={},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 503
    # Cooldown NOT recorded — user must be able to retry.
    assert user_id not in _REQUEST_LIVE_COOLDOWN


async def test_request_live_rejects_oversized_note(
    client, db_session, burner, monkeypatch, _bot_enabled_with_operator_chat
):
    """500-char cap. Above that, 422 from pydantic — the bot message
    stays bounded regardless of what the user submits."""
    token, _ = await _bind_and_get_token(client, burner)
    r = await client.post(
        "/api/wallet/request-live",
        json={"note": "x" * 501},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 422


async def test_request_live_concurrent_requests_single_message(
    client, db_session, burner, monkeypatch, _bot_enabled_with_operator_chat
):
    """Race regression: two concurrent requests for the same user MUST
    produce at most ONE Telegram message. Without reserving the
    cooldown slot before the `await telegram_client.send_message`,
    both requests would pass the cooldown check (cache.get → None
    for both), both await the send, both succeed, both set the
    cooldown — operator receives 2 messages.

    Fix: reserve the cooldown slot BEFORE the await; release on
    Telegram failure. This test holds a real (but mocked) Telegram
    send open for both requests, then runs them concurrently, and
    asserts the operator sees exactly one message.
    """
    import asyncio
    from unittest.mock import AsyncMock

    from etfpulse.adapters import telegram as telegram_mod

    sent: list[dict] = []
    # Gate the mocked send so both requests can pile up at the await
    # point — simulates a slow upstream where the race window is
    # actually large.
    release = asyncio.Event()

    async def slow_send(*, chat_id, text, parse_mode, reply_markup=None):
        await release.wait()
        sent.append({"chat_id": chat_id})

    monkeypatch.setattr(telegram_mod.telegram_client, "send_message", AsyncMock(side_effect=slow_send))

    token, _ = await _bind_and_get_token(client, burner)

    async def fire():
        return await client.post(
            "/api/wallet/request-live",
            json={},
            headers={"Authorization": f"Bearer {token}"},
        )

    # Fire two requests in parallel; both park at `await send_message`.
    task1 = asyncio.create_task(fire())
    task2 = asyncio.create_task(fire())
    # Let both tasks enter the route + reach the await.
    await asyncio.sleep(0.05)
    # Unblock both sends.
    release.set()
    r1, r2 = await asyncio.gather(task1, task2)

    # Exactly ONE 200 + ONE 429. The race-protected slot ensures the
    # second request sees the cooldown set BEFORE the first's send
    # completes.
    statuses = sorted([r1.status_code, r2.status_code])
    assert statuses == [200, 429], f"expected one 200 + one 429, got {statuses}"
    assert len(sent) == 1, f"expected exactly 1 Telegram message, got {len(sent)}"


def test_request_live_cooldown_cache_ttl_covers_max_setting():
    """Invariant: the cache TTL must be at least as long as the maximum
    possible value of `settings.request_live_cooldown_seconds`. If the
    settings `le=` bound is raised in config.py without raising the
    cache TTL constant, this test fails LOUDLY — preventing a silent
    gap where the cache evicts before the cooldown expires.

    Catches the bug class: handler asks the cache "when did you last
    submit?" — cache says "no record" because it evicted — handler
    thinks no cooldown applies — operator gets spammed.
    """
    from pydantic.fields import FieldInfo

    from etfpulse.api.routes.wallet import (
        _REQUEST_LIVE_COOLDOWN,
        _REQUEST_LIVE_COOLDOWN_MAX_TTL_SECONDS,
    )
    from etfpulse.config import Settings

    field: FieldInfo = Settings.model_fields["request_live_cooldown_seconds"]
    # Walk pydantic's metadata for the `le` constraint (le=86400 in config.py).
    le_values = [m.le for m in field.metadata if hasattr(m, "le") and m.le is not None]
    assert le_values, "request_live_cooldown_seconds must declare an `le=` upper bound"
    settings_max = int(le_values[0])

    assert _REQUEST_LIVE_COOLDOWN_MAX_TTL_SECONDS >= settings_max, (
        f"Cache TTL ({_REQUEST_LIVE_COOLDOWN_MAX_TTL_SECONDS}s) must be >= "
        f"settings.request_live_cooldown_seconds upper bound ({settings_max}s). "
        f"Raise _REQUEST_LIVE_COOLDOWN_MAX_TTL_SECONDS in wallet.py or lower "
        f"the Field(le=...) in config.py."
    )
    # Belt: the live cache instance MUST be constructed with this TTL.
    assert _REQUEST_LIVE_COOLDOWN.ttl == _REQUEST_LIVE_COOLDOWN_MAX_TTL_SECONDS


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


async def test_verify_vanished_user_jwt_falls_through_and_logs(
    client, db_session, burner, monkeypatch
):
    """#78.8 — JWT is valid (signature OK, not expired, audience matches)
    but the User row it points at is gone (mint→delete race, manual
    admin DELETE, DB restore, etc). The verify route silently falls
    through to the anonymous path, AND emits a `log.warning` so
    operators can see if this fires more than rarely.

    Pins both:
      1. Behavior — the request succeeds and creates a fresh user keyed
         by the SIWE-verified wallet. The stale JWT doesn't block the
         legitimate first-bind.
      2. Observability — `wallet_verify_authed_user_vanished` warning
         emitted with the vanished user_id. A future refactor that
         removes the log line should fail this test.
    """
    from unittest.mock import MagicMock

    from etfpulse.api.auth import mint_jwt
    from etfpulse.api.routes import wallet as wallet_route

    # Pre-create a user, mint a JWT for them, then delete the row before
    # the verify call lands. `db_session` is shared with the route via
    # the `client` fixture's dependency override.
    ghost = User(wallet_address=None)
    db_session.add(ghost)
    await db_session.flush()
    ghost_id = ghost.id
    jwt = mint_jwt(ghost_id)
    await db_session.delete(ghost)
    await db_session.flush()

    # Spy the wallet route's `log` so we can assert .warning was called.
    # Direct attribute swap on the module — the route binds `log` at
    # module load, so monkeypatching here replaces it process-wide for
    # the test's duration.
    mock_log = MagicMock()
    monkeypatch.setattr(wallet_route, "log", mock_log)

    nonce_body = await _request_nonce(client, burner.address)
    message, signature = _build_signed_siwe(burner, nonce=nonce_body["nonce"])
    r = await client.post(
        "/api/wallet/verify",
        json={"message": message, "signature": signature},
        headers={"Authorization": f"Bearer {jwt}"},
    )
    # Behavior: request succeeds, a new user is created keyed by wallet.
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["wallet_address"] == burner.address.lower()
    assert body["user_id"] != ghost_id  # fresh row, not the deleted ghost

    # Observability: warning emitted with the vanished user_id.
    warning_calls = [c for c in mock_log.warning.call_args_list if c.args]
    assert any(
        call.args[0] == "wallet_verify_authed_user_vanished"
        and call.kwargs.get("user_id") == ghost_id
        for call in warning_calls
    ), (
        f"expected wallet_verify_authed_user_vanished log.warning with "
        f"user_id={ghost_id}, got calls: {mock_log.warning.call_args_list}"
    )


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
