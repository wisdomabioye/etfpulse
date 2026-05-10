"""Bot StartupTask — gate behaviour, webhook registration, shutdown timing.

We mock PTB's `Application` + `telegram_client.set_webhook` so tests don't
hit Telegram's API. The value here is in verifying the gate logic and
lifespan integration — PTB itself is assumed-working.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from telegram.error import NetworkError

from etfpulse.bot.lifespan import start_bot
from etfpulse.config import settings


@pytest.fixture
def enable_bot(monkeypatch):
    """Set all four telegram fields + run_bot=True so `is_bot_enabled` passes."""
    monkeypatch.setattr(settings, "run_bot", True)
    monkeypatch.setattr(settings, "telegram_bot_token", "12345:test-token")
    monkeypatch.setattr(settings, "telegram_public_url", "https://app.example.com")
    monkeypatch.setattr(settings, "telegram_webhook_secret", "s3cr3t")
    monkeypatch.setattr(settings, "telegram_webhook_url_suffix", "abc123xyz")


@pytest.fixture
def mock_application(monkeypatch):
    """Replace `Application.builder().token().build()` with a mock. All PTB
    lifecycle methods become AsyncMocks we can assert against."""
    app_mock = MagicMock()
    app_mock.initialize = AsyncMock()
    app_mock.shutdown = AsyncMock()
    app_mock.add_handler = MagicMock()

    builder = MagicMock()
    builder.token.return_value = builder
    builder.build.return_value = app_mock

    # Patch Application.builder() in the lifespan module's namespace.
    monkeypatch.setattr("etfpulse.bot.lifespan.Application.builder", lambda: builder)
    return app_mock


@pytest.fixture
def mock_set_webhook(monkeypatch):
    """Replace `telegram_client.set_webhook` with an AsyncMock."""
    stub = AsyncMock(return_value=True)
    monkeypatch.setattr("etfpulse.bot.lifespan.telegram_client.set_webhook", stub)
    return stub


# ---- Gate behaviour -------------------------------------------------------


@pytest.mark.parametrize(
    "missing_field",
    [
        "run_bot",
        "telegram_bot_token",
        "telegram_public_url",
        "telegram_webhook_secret",
        "telegram_webhook_url_suffix",
    ],
)
async def test_disabled_when_any_required_field_missing(
    missing_field, enable_bot, mock_application, mock_set_webhook, monkeypatch
):
    """Every required field is necessary. Flip any one to falsy → bot stays off."""
    if missing_field == "run_bot":
        monkeypatch.setattr(settings, "run_bot", False)
    else:
        monkeypatch.setattr(settings, missing_field, "")

    app = FastAPI()
    async with start_bot(app):
        assert not hasattr(app.state, "bot_application")

    # No PTB init, no webhook registration.
    mock_application.initialize.assert_not_awaited()
    mock_set_webhook.assert_not_awaited()


# ---- Enabled path --------------------------------------------------------


async def test_enabled_initializes_and_registers_webhook(
    enable_bot, mock_application, mock_set_webhook
):
    app = FastAPI()
    async with start_bot(app):
        # Application was initialized exactly once.
        mock_application.initialize.assert_awaited_once()
        # Webhook registered with the assembled URL + our allowed updates.
        mock_set_webhook.assert_awaited_once_with(
            url="https://app.example.com/api/telegram/webhook/abc123xyz",
            secret_token="s3cr3t",
            allowed_updates=["message", "my_chat_member", "callback_query"],
        )
        # Application stashed on app.state for the webhook receiver.
        assert app.state.bot_application is mock_application


async def test_shutdown_calls_application_shutdown(enable_bot, mock_application, mock_set_webhook):
    app = FastAPI()
    async with start_bot(app):
        pass

    mock_application.shutdown.assert_awaited_once()


async def test_public_url_trailing_slash_stripped(
    enable_bot, mock_application, mock_set_webhook, monkeypatch
):
    """Operators sometimes paste URLs with a trailing slash. We strip it so
    we don't register `https://app/api/telegram/webhook//abc` with a double
    slash that would 404."""
    monkeypatch.setattr(settings, "telegram_public_url", "https://app.example.com/")

    app = FastAPI()
    async with start_bot(app):
        pass

    call_kwargs = mock_set_webhook.await_args.kwargs
    assert call_kwargs["url"] == "https://app.example.com/api/telegram/webhook/abc123xyz"


# ---- Resilience ----------------------------------------------------------


async def test_set_webhook_failure_does_not_block_startup(
    enable_bot, mock_application, mock_set_webhook
):
    """Telegram unreachable during deploy → bot still boots. Pin this so
    a future refactor doesn't regress to "block startup on upstream"."""
    mock_set_webhook.side_effect = NetworkError("telegram unreachable")

    app = FastAPI()
    async with start_bot(app):
        # Despite set_webhook failing, the Application was initialized and
        # attached to app.state — the bot is functional locally. Webhook
        # registration will retry on next container restart.
        mock_application.initialize.assert_awaited_once()
        assert app.state.bot_application is mock_application


async def test_initialize_failure_does_not_block_startup(
    enable_bot, mock_application, mock_set_webhook
):
    """Discovered during Stage 5e smoke — PTB's `Application.initialize()`
    calls `getMe` against Telegram. With an invalid token or an API outage
    during deploy, this raises a PTBTelegramError and previously propagated
    through the lifespan, preventing app boot.

    New contract: log + yield + return WITHOUT attaching `app.state.bot_application`,
    so the webhook route returns 404 until the next container restart retries.
    """
    from telegram.error import InvalidToken

    mock_application.initialize.side_effect = InvalidToken("fake token")

    app = FastAPI()
    async with start_bot(app):
        # Bot is "disabled at runtime" — webhook route would 404.
        assert not hasattr(app.state, "bot_application")
        # set_webhook must NOT have been called (would fail the same way).
        mock_set_webhook.assert_not_awaited()


# ---- Timing --------------------------------------------------------------


async def test_shutdown_under_5_seconds(enable_bot, mock_application, mock_set_webhook):
    """Coolify deploys timeout fast; bot teardown must be near-instant."""
    app = FastAPI()

    start = time.monotonic()
    async with start_bot(app):
        pass
    elapsed = time.monotonic() - start

    assert elapsed < 5.0, f"shutdown took {elapsed:.2f}s — should be sub-second"


# ---- Registration in STARTUP_TASKS ---------------------------------------


def test_start_bot_registered_in_startup_tasks():
    """Guardrail — same pattern as #46's `test_scheduler_registered_at_import`.
    If a refactor breaks the append, bot would silently never boot in prod."""
    from etfpulse.api import lifespan as lifespan_mod

    assert start_bot in lifespan_mod.STARTUP_TASKS
