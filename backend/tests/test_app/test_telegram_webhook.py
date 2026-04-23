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
