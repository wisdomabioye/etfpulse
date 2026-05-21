"""PR D.5.1 — Telegram WebApp HMAC verifier + JWT mint route tests.

Pins:
  - Pure verifier: happy path, hash tampered, missing hash, malformed
    init_data, expired auth_date, missing/malformed user field,
    missing/malformed auth_date, empty bot_token defensive belt.
  - Route: happy path (new user creation + JWT mint), bot-disabled 404,
    repeat verify reuses existing User row (no duplicate), shared
    helper produces same User for tg-bot DM-bound and WebApp-bound
    paths (convergence guarantee).

Golden HMAC is synthesised in-test via stdlib hmac — same primitives
the verifier uses. The test never reaches Telegram; it constructs
initData exactly the way Telegram would have, signs it with the
known bot_token, then passes it to the verifier.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import AsyncIterator
from urllib.parse import urlencode

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from etfpulse.api.auth_telegram import (
    WebAppVerifyError,
    verify_webapp_init_data,
)
from etfpulse.api.deps import get_db_session
from etfpulse.app import create_app
from etfpulse.config import settings
from etfpulse.identity import resolve_or_create_user_by_tg_id
from etfpulse.models import ChannelType, NotificationChannel, User

_BOT_TOKEN = "1234567890:TEST_BOT_TOKEN"


def _build_init_data(
    *,
    bot_token: str = _BOT_TOKEN,
    tg_user_id: int = 12345,
    username: str | None = "tester",
    auth_date: int | None = None,
    tamper_hash: bool = False,
    omit_hash: bool = False,
    omit_user: bool = False,
    omit_auth_date: bool = False,
    malformed_user: bool = False,
    malformed_auth_date: bool = False,
) -> str:
    """Synthesise initData the way Telegram would.

    All fields ASCII-sorted before HMAC, then URL-encoded for the
    wire form. Tamper flags let one test exercise each failure mode.
    """
    user = {"id": tg_user_id, "first_name": "Test"}
    if username is not None:
        user["username"] = username

    fields: dict[str, str] = {}
    if not omit_auth_date:
        fields["auth_date"] = (
            "not-a-number"
            if malformed_auth_date
            else str(auth_date if auth_date is not None else int(time.time()))
        )
    fields["query_id"] = "qid_abc123"
    if not omit_user:
        fields["user"] = "not-json" if malformed_user else json.dumps(user)

    # Compute hash over the sorted k=v\n... string (NO URL-decoding —
    # Telegram signs the post-decode form per spec).
    data_check_string = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    sig = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not omit_hash:
        fields["hash"] = "0" * len(sig) if tamper_hash else sig

    # Wire form is URL-encoded query-string. `urlencode` is the inverse
    # of `parse_qsl` which the verifier uses.
    return urlencode(fields)


# ---------------------------------------------------------------------------
# Pure verifier tests
# ---------------------------------------------------------------------------


def test_verify_happy_path():
    raw = _build_init_data()
    user = verify_webapp_init_data(raw, bot_token=_BOT_TOKEN, max_age_seconds=600)
    assert user["id"] == 12345
    assert user["username"] == "tester"
    assert user["first_name"] == "Test"


def test_verify_rejects_tampered_hash():
    raw = _build_init_data(tamper_hash=True)
    with pytest.raises(WebAppVerifyError, match="invalid hash"):
        verify_webapp_init_data(raw, bot_token=_BOT_TOKEN, max_age_seconds=600)


def test_verify_rejects_missing_hash():
    raw = _build_init_data(omit_hash=True)
    with pytest.raises(WebAppVerifyError, match="missing hash"):
        verify_webapp_init_data(raw, bot_token=_BOT_TOKEN, max_age_seconds=600)


def test_verify_rejects_expired_auth_date():
    # Synthesise a 30-minute-old payload; 600s window → reject.
    old = int(time.time()) - 30 * 60
    raw = _build_init_data(auth_date=old)
    with pytest.raises(WebAppVerifyError, match="expired"):
        verify_webapp_init_data(raw, bot_token=_BOT_TOKEN, max_age_seconds=600)


def test_verify_rejects_missing_auth_date():
    raw = _build_init_data(omit_auth_date=True)
    with pytest.raises(WebAppVerifyError, match="auth_date"):
        verify_webapp_init_data(raw, bot_token=_BOT_TOKEN, max_age_seconds=600)


def test_verify_rejects_malformed_auth_date():
    raw = _build_init_data(malformed_auth_date=True)
    with pytest.raises(WebAppVerifyError, match="malformed auth_date"):
        verify_webapp_init_data(raw, bot_token=_BOT_TOKEN, max_age_seconds=600)


def test_verify_rejects_missing_user():
    raw = _build_init_data(omit_user=True)
    with pytest.raises(WebAppVerifyError, match="user"):
        verify_webapp_init_data(raw, bot_token=_BOT_TOKEN, max_age_seconds=600)


def test_verify_rejects_malformed_user_json():
    raw = _build_init_data(malformed_user=True)
    with pytest.raises(WebAppVerifyError, match="malformed user"):
        verify_webapp_init_data(raw, bot_token=_BOT_TOKEN, max_age_seconds=600)


def test_verify_rejects_wrong_bot_token():
    """Hash was computed over `_BOT_TOKEN`; verify with a different
    bot_token → recompute differs → hash mismatch."""
    raw = _build_init_data()
    with pytest.raises(WebAppVerifyError, match="invalid hash"):
        verify_webapp_init_data(raw, bot_token="different:token", max_age_seconds=600)


def test_verify_rejects_empty_bot_token_defensive_belt():
    """The route's `is_bot_enabled` gate should 404 before this branch
    fires; defensive check ensures a misuse from a different caller
    fails loud rather than producing a hash over empty key."""
    raw = _build_init_data()
    with pytest.raises(WebAppVerifyError, match="bot not configured"):
        verify_webapp_init_data(raw, bot_token="", max_age_seconds=600)


def test_verify_rejects_malformed_init_data_string():
    with pytest.raises(WebAppVerifyError, match="missing init_data"):
        verify_webapp_init_data("", bot_token=_BOT_TOKEN, max_age_seconds=600)


def test_verify_freshness_uses_injected_now():
    """`now` injection: test stable across clock — pass `now` matching
    the auth_date so age=0 regardless of wall clock."""
    auth_date = 1_700_000_000  # arbitrary past timestamp
    raw = _build_init_data(auth_date=auth_date)
    user = verify_webapp_init_data(
        raw,
        bot_token=_BOT_TOKEN,
        max_age_seconds=600,
        now=auth_date + 60,  # 60s old; within window
    )
    assert user["id"] == 12345


def test_verify_rejects_zero_user_id():
    """`user.id = 0` would mint a JWT for user_id=0 which `mint_jwt`
    rejects (per D.4.1). Defensive belt at the verifier."""
    raw = _build_init_data(tg_user_id=0)
    with pytest.raises(WebAppVerifyError, match="malformed user id"):
        verify_webapp_init_data(raw, bot_token=_BOT_TOKEN, max_age_seconds=600)


# ---------------------------------------------------------------------------
# Route tests
# ---------------------------------------------------------------------------


@pytest.fixture
async def app_and_client(
    db_session: AsyncSession,
    monkeypatch,
) -> AsyncIterator[tuple[FastAPI, httpx.AsyncClient]]:
    """Real FastAPI app + DB-session override + bot token / webhook
    config pinned so `is_bot_enabled` is True."""
    # `is_bot_enabled` requires all 4 of these.
    monkeypatch.setattr(settings, "run_bot", True)
    monkeypatch.setattr(settings, "telegram_bot_token", _BOT_TOKEN)
    monkeypatch.setattr(settings, "telegram_public_url", "https://etfpulse.example.com")
    monkeypatch.setattr(settings, "telegram_webhook_secret", "x" * 32)
    monkeypatch.setattr(settings, "telegram_webhook_url_suffix", "abc123")
    monkeypatch.setattr(settings, "webapp_init_data_max_age_seconds", 600)

    app = create_app()

    async def _override_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db_session] = _override_session

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as c:
        yield app, c


async def test_route_happy_path_creates_user(app_and_client, db_session):
    _, client = app_and_client
    raw = _build_init_data(tg_user_id=99001, username="newbie")
    r = await client.post("/api/auth/telegram/verify", json={"init_data": raw})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["telegram_user_id"] == 99001
    assert body["has_wallet"] is False
    assert body["jwt"]
    # Validate the NotificationChannel was created with str(tg_user_id)
    # — same key shape as the bot's DM flow.
    result = await db_session.execute(
        select(NotificationChannel).where(
            NotificationChannel.channel_type == ChannelType.TELEGRAM.value,
            NotificationChannel.channel_identifier == "99001",
        )
    )
    channel = result.scalar_one()
    assert channel.user_id == body["user_id"]


async def test_route_404_when_bot_disabled(db_session, monkeypatch):
    """`is_bot_enabled` requires all 4 telegram fields set; clearing
    one falsifies it. Route then returns 404 (info-leak policy)."""
    monkeypatch.setattr(settings, "run_bot", True)
    monkeypatch.setattr(settings, "telegram_bot_token", "")
    monkeypatch.setattr(settings, "telegram_public_url", "https://etfpulse.example.com")
    monkeypatch.setattr(settings, "telegram_webhook_secret", "x" * 32)
    monkeypatch.setattr(settings, "telegram_webhook_url_suffix", "abc123")

    app = create_app()

    async def _override_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db_session] = _override_session
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as c:
        r = await c.post(
            "/api/auth/telegram/verify",
            json={"init_data": "any=string"},
        )
    assert r.status_code == 404


async def test_route_reverify_returns_same_user(app_and_client, db_session):
    """Two verify calls for the same tg_user_id → SAME User row.
    The shared helper's race-safe upsert guarantees one row per
    Telegram id."""
    _, client = app_and_client
    raw1 = _build_init_data(tg_user_id=99002, username="repeat")
    r1 = await client.post("/api/auth/telegram/verify", json={"init_data": raw1})
    assert r1.status_code == 200
    user_id_1 = r1.json()["user_id"]

    raw2 = _build_init_data(tg_user_id=99002, username="repeat")
    r2 = await client.post("/api/auth/telegram/verify", json={"init_data": raw2})
    assert r2.status_code == 200
    user_id_2 = r2.json()["user_id"]
    assert user_id_1 == user_id_2


async def test_route_400_on_tampered_hash(app_and_client):
    _, client = app_and_client
    raw = _build_init_data(tamper_hash=True)
    r = await client.post("/api/auth/telegram/verify", json={"init_data": raw})
    assert r.status_code == 400
    assert r.json()["detail"] == "invalid hash"


async def test_route_400_on_expired_init_data(app_and_client):
    _, client = app_and_client
    raw = _build_init_data(auth_date=int(time.time()) - 30 * 60)
    r = await client.post("/api/auth/telegram/verify", json={"init_data": raw})
    assert r.status_code == 400
    assert "expired" in r.json()["detail"]


async def test_route_422_on_missing_body_field(app_and_client):
    _, client = app_and_client
    r = await client.post("/api/auth/telegram/verify", json={})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Convergence guarantee: bot DM + WebApp share the same User row
# ---------------------------------------------------------------------------


async def test_shared_helper_convergence(db_session):
    """The shared `resolve_or_create_user_by_tg_id` is used by BOTH:
      - bot's `/start` DM flow (via `_resolve_or_create_user`)
      - the WebApp verifier route

    Both pass `tg_user_id` from their respective sources. For DM,
    `chat.id == tg_user.id` per Telegram. So both code paths should
    converge on the same `User` + `NotificationChannel` row.

    This test calls the shared helper directly twice with the same
    id and asserts the same row is returned — pins the upsert
    idempotency that the convergence guarantee rests on.
    """
    tg_id = 99003
    user_a = await resolve_or_create_user_by_tg_id(db_session, tg_user_id=tg_id, username="first")
    await db_session.flush()
    user_b = await resolve_or_create_user_by_tg_id(
        db_session, tg_user_id=tg_id, username="second_call_ignored"
    )
    assert user_a.id == user_b.id

    # The pre-existing channel keeps its original username (we don't
    # update on hit).
    result = await db_session.execute(
        select(NotificationChannel).where(
            NotificationChannel.channel_type == ChannelType.TELEGRAM.value,
            NotificationChannel.channel_identifier == str(tg_id),
        )
    )
    channel = result.scalar_one()
    assert channel.username == "first"


async def test_shared_helper_new_user_has_delivery_defaults(db_session):
    """New User created via the helper picks up env-driven defaults
    (pref_assets, pref_min_confidence). Pinned because future config
    changes that drop these defaults would silently break new-user
    onboarding."""
    tg_id = 99004
    user = await resolve_or_create_user_by_tg_id(db_session, tg_user_id=tg_id, username="defaults")
    assert user.pref_assets == settings.delivery_default_assets_list
    assert user.pref_min_confidence == settings.delivery_default_min_confidence
    assert user.wallet_address is None
    # Belt — the helper returns a User row; touch a field other than
    # the ones above to ensure the row was actually inserted.
    assert isinstance(user.id, int) and user.id > 0


async def test_helper_user_row_query_after_create(db_session):
    """Sanity: after the helper inserts a fresh row, it's retrievable
    via `session.get`. Pins that the flush actually committed."""
    tg_id = 99005
    user = await resolve_or_create_user_by_tg_id(db_session, tg_user_id=tg_id, username="check")
    refetched = await db_session.get(User, user.id)
    assert refetched is not None
    assert refetched.id == user.id
