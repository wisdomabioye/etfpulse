"""Telegram webhook receiver — 3-gate chain + dispatch to PTB.

Critical invariant: wrong suffix and bot-disabled BOTH return 404 so scanners
can't use response codes to map the endpoint. Only the 401 path (right
suffix + bot up + wrong secret) can reveal the URL is active, and that
requires the attacker to already know the suffix.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from etfpulse.app import create_app
from etfpulse.config import settings

_SUFFIX = "abc123xyz-secret-suffix"
_SECRET = "telegram-header-secret"
_WEBHOOK_PATH = f"/api/telegram/webhook/{_SUFFIX}"


_MIN_UPDATE_PAYLOAD = {
    "update_id": 1,
    "message": {
        "message_id": 1,
        "date": 1700000000,
        "chat": {"id": 42, "type": "private"},
        "from": {"id": 42, "is_bot": False, "first_name": "Test"},
        "text": "/start",
    },
}


@pytest.fixture
def enable_bot(monkeypatch):
    """All four telegram fields set so `is_bot_enabled` passes."""
    monkeypatch.setattr(settings, "run_bot", True)
    monkeypatch.setattr(settings, "telegram_bot_token", "12345:test")
    monkeypatch.setattr(settings, "telegram_public_url", "https://app.example.com")
    monkeypatch.setattr(settings, "telegram_webhook_secret", _SECRET)
    monkeypatch.setattr(settings, "telegram_webhook_url_suffix", _SUFFIX)


@pytest.fixture
def mock_application(monkeypatch):
    """Replace PTB Application builder + set_webhook so full app boot works
    without hitting Telegram."""
    app_mock = MagicMock()
    app_mock.initialize = AsyncMock()
    app_mock.shutdown = AsyncMock()
    app_mock.process_update = AsyncMock()
    app_mock.bot = MagicMock()

    builder = MagicMock()
    builder.token.return_value = builder
    builder.build.return_value = app_mock

    monkeypatch.setattr("etfpulse.bot.lifespan.Application.builder", lambda: builder)
    monkeypatch.setattr(
        "etfpulse.bot.lifespan.telegram_client.set_webhook",
        AsyncMock(return_value=True),
    )
    # PTB's Update.de_json needs a real-looking bot; return a minimal mock
    # Update so process_update doesn't care about parsing.
    monkeypatch.setattr(
        "etfpulse.api.routes.telegram.Update.de_json",
        lambda data, bot: MagicMock(update_id=data.get("update_id")),
    )
    return app_mock


# ---- 3-gate chain --------------------------------------------------------


def test_wrong_suffix_returns_404(enable_bot, mock_application):
    """Scanner hitting an unknown suffix must see plain 404."""
    with TestClient(create_app()) as client:
        r = client.post(
            "/api/telegram/webhook/wrong-suffix",
            json=_MIN_UPDATE_PAYLOAD,
            headers={"X-Telegram-Bot-Api-Secret-Token": _SECRET},
        )
    assert r.status_code == 404
    mock_application.process_update.assert_not_awaited()


def test_bot_disabled_returns_404(monkeypatch, mock_application):
    """Bot disabled (run_bot=False) → route absent from scanner's POV.
    Even with right suffix and right secret, it's still 404."""
    # Leave run_bot=False (autouse default); don't apply enable_bot.
    # Other fields also absent → is_bot_enabled returns False → startup skip.
    with TestClient(create_app()) as client:
        r = client.post(
            _WEBHOOK_PATH,
            json=_MIN_UPDATE_PAYLOAD,
            headers={"X-Telegram-Bot-Api-Secret-Token": _SECRET},
        )
    assert r.status_code == 404
    mock_application.process_update.assert_not_awaited()


def test_missing_secret_header_returns_401(enable_bot, mock_application):
    """Right suffix, bot up, no secret header → 401.
    This is the only 4xx code that reveals the endpoint is real."""
    with TestClient(create_app()) as client:
        r = client.post(_WEBHOOK_PATH, json=_MIN_UPDATE_PAYLOAD)
    assert r.status_code == 401
    mock_application.process_update.assert_not_awaited()


def test_wrong_secret_returns_401(enable_bot, mock_application):
    with TestClient(create_app()) as client:
        r = client.post(
            _WEBHOOK_PATH,
            json=_MIN_UPDATE_PAYLOAD,
            headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
        )
    assert r.status_code == 401
    mock_application.process_update.assert_not_awaited()


# ---- Happy path ----------------------------------------------------------


def test_valid_request_dispatches_and_returns_200(enable_bot, mock_application):
    """All three gates pass → process_update called, 200 returned."""
    with TestClient(create_app()) as client:
        r = client.post(
            _WEBHOOK_PATH,
            json=_MIN_UPDATE_PAYLOAD,
            headers={"X-Telegram-Bot-Api-Secret-Token": _SECRET},
        )
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    mock_application.process_update.assert_awaited_once()


def test_gate_order_suffix_check_precedes_secret(enable_bot, mock_application):
    """Wrong suffix AND wrong secret → 404 (suffix check wins first).
    Proves a scanner can't use wrong-secret probing to distinguish good/bad URLs."""
    with TestClient(create_app()) as client:
        r = client.post(
            "/api/telegram/webhook/wrong",
            json=_MIN_UPDATE_PAYLOAD,
            headers={"X-Telegram-Bot-Api-Secret-Token": "also-wrong"},
        )
    assert r.status_code == 404


# ---- Hot rotation (issue #40) --------------------------------------------


def test_rotate_disabled_when_admin_key_empty(enable_bot, mock_application, monkeypatch):
    monkeypatch.setattr(settings, "admin_api_key", "")
    with TestClient(create_app()) as client:
        r = client.post("/api/admin/telegram/rotate-webhook-secret")
    assert r.status_code == 503


def test_rotate_requires_correct_admin_key(enable_bot, mock_application, monkeypatch):
    monkeypatch.setattr(settings, "admin_api_key", "secret-key")
    with TestClient(create_app()) as client:
        r = client.post(
            "/api/admin/telegram/rotate-webhook-secret",
            headers={"X-Admin-Key": "wrong"},
        )
    assert r.status_code == 401


def test_rotate_returns_503_when_bot_disabled(monkeypatch, mock_application):
    """Bot off → bot_application not attached → nothing to rotate."""
    monkeypatch.setattr(settings, "admin_api_key", "secret-key")
    # Leave bot disabled (autouse default).
    with TestClient(create_app()) as client:
        r = client.post(
            "/api/admin/telegram/rotate-webhook-secret",
            headers={"X-Admin-Key": "secret-key"},
        )
    assert r.status_code == 503
    assert "bot disabled" in r.json()["detail"]


def test_rotate_generates_secret_and_updates_state(enable_bot, mock_application, monkeypatch):
    """Happy path: no body → server generates → set_webhook called → state
    shrinks to {new}. The old secret no longer verifies."""
    monkeypatch.setattr(settings, "admin_api_key", "secret-key")

    with TestClient(create_app()) as client:
        # `mock_application` fixture patched telegram_client.set_webhook on
        # the shared singleton; lifespan called it once at boot. Reset so
        # we observe only the rotate call.
        from etfpulse.adapters.telegram import telegram_client as live_client

        live_client.set_webhook.reset_mock()

        r = client.post(
            "/api/admin/telegram/rotate-webhook-secret",
            headers={"X-Admin-Key": "secret-key"},
        )
        assert r.status_code == 200
        body = r.json()
        new_secret = body["secret"]
        assert len(new_secret) == 64  # token_hex(32) → 64 hex chars
        assert "TELEGRAM_WEBHOOK_SECRET" in body["note"]

        live_client.set_webhook.assert_awaited_once()
        kwargs = live_client.set_webhook.await_args.kwargs
        assert kwargs["secret_token"] == new_secret
        assert kwargs["allowed_updates"] == ["message", "my_chat_member"]

        # OLD secret should now be rejected by the webhook route.
        r_old = client.post(
            _WEBHOOK_PATH,
            json=_MIN_UPDATE_PAYLOAD,
            headers={"X-Telegram-Bot-Api-Secret-Token": _SECRET},
        )
        assert r_old.status_code == 401

        # NEW secret accepted.
        r_new = client.post(
            _WEBHOOK_PATH,
            json=_MIN_UPDATE_PAYLOAD,
            headers={"X-Telegram-Bot-Api-Secret-Token": new_secret},
        )
        assert r_new.status_code == 200


def test_rotate_accepts_operator_supplied_secret(enable_bot, mock_application, monkeypatch):
    monkeypatch.setattr(settings, "admin_api_key", "secret-key")
    operator_secret = "a" * 64

    with TestClient(create_app()) as client:
        r = client.post(
            "/api/admin/telegram/rotate-webhook-secret",
            headers={"X-Admin-Key": "secret-key"},
            json={"secret": operator_secret},
        )
        assert r.status_code == 200
        assert r.json()["secret"] == operator_secret


def test_rotate_rejects_short_secret(enable_bot, mock_application, monkeypatch):
    """Pydantic enforces >= 32 chars on operator-supplied secrets."""
    monkeypatch.setattr(settings, "admin_api_key", "secret-key")
    with TestClient(create_app()) as client:
        r = client.post(
            "/api/admin/telegram/rotate-webhook-secret",
            headers={"X-Admin-Key": "secret-key"},
            json={"secret": "tooshort"},
        )
    assert r.status_code == 422  # FastAPI validation error


def test_rotate_reverts_on_set_webhook_failure(enable_bot, mock_application, monkeypatch):
    """If Telegram is unreachable: state reverts to {old}, old secret keeps
    working, response is 502."""
    from telegram.error import TelegramError

    monkeypatch.setattr(settings, "admin_api_key", "secret-key")

    with TestClient(create_app()) as client:
        # Swap the shared singleton's set_webhook to raise — same target as
        # `mock_application` fixture, but our error variant overrides.
        from etfpulse.adapters.telegram import telegram_client as live_client

        live_client.set_webhook = AsyncMock(side_effect=TelegramError("upstream down"))

        r = client.post(
            "/api/admin/telegram/rotate-webhook-secret",
            headers={"X-Admin-Key": "secret-key"},
        )
        assert r.status_code == 502
        assert "reverted" in r.json()["detail"]

        # Old secret still valid — rotation was atomic / reverted cleanly.
        r_old = client.post(
            _WEBHOOK_PATH,
            json=_MIN_UPDATE_PAYLOAD,
            headers={"X-Telegram-Bot-Api-Secret-Token": _SECRET},
        )
        assert r_old.status_code == 200


def test_rotate_lock_installed_at_boot(enable_bot, mock_application):
    """Boot wires an asyncio.Lock onto app.state. The route's defensive
    503 path (covered by `test_rotate_returns_503_when_lock_missing`)
    fires when this is absent — pinning the boot invariant prevents a
    future refactor from silently dropping the lock without breaking
    visible behaviour."""
    import asyncio as asyncio_mod

    with TestClient(create_app()) as client:
        lock = client.app.state.telegram_webhook_rotate_lock
        assert isinstance(lock, asyncio_mod.Lock)


def test_rotate_returns_503_when_lock_missing(enable_bot, mock_application, monkeypatch):
    """Defensive branch — if a future change forgets to install the lock,
    the route refuses rather than proceed unserialised."""
    monkeypatch.setattr(settings, "admin_api_key", "secret-key")

    with TestClient(create_app()) as client:
        client.app.state.telegram_webhook_rotate_lock = None

        r = client.post(
            "/api/admin/telegram/rotate-webhook-secret",
            headers={"X-Admin-Key": "secret-key"},
        )
    assert r.status_code == 503
    assert "lock uninitialised" in r.json()["detail"]
