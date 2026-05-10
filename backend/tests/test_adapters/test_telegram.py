"""Telegram adapter — error mapping, idempotent webhook registration,
no-token semantics.

We mock PTB's `Bot` rather than hitting the real Telegram API. The adapter
is a thin wrapper, so most of the value is in verifying the error-class
translation and the idempotency check.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.error import BadRequest, Forbidden, NetworkError

from etfpulse.adapters.telegram import (
    TelegramBlockedError,
    TelegramChatNotFoundError,
    TelegramClient,
    TelegramError,
)
from etfpulse.config import settings


@pytest.fixture
def mock_bot(monkeypatch):
    """Replace `TelegramClient._bot` with a MagicMock whose async methods
    return AsyncMocks. Per-test overrides the return values / side effects."""
    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.get_webhook_info = AsyncMock()
    bot.set_webhook = AsyncMock()
    bot.delete_webhook = AsyncMock()

    monkeypatch.setattr(TelegramClient, "_bot", lambda self: bot)
    return bot


@pytest.fixture
def with_token(monkeypatch):
    monkeypatch.setattr(settings, "telegram_bot_token", "12345:test-token")


def _stub_message(message_id: int = 42, chat_id: int = 999):
    """Mock PTB Message with just the fields our adapter reads."""
    msg = MagicMock()
    msg.message_id = message_id
    msg.chat.id = chat_id
    return msg


# ---- send_message — happy path -------------------------------------------


async def test_send_message_returns_sent_message(mock_bot, with_token):
    mock_bot.send_message.return_value = _stub_message(message_id=42, chat_id=999)

    client = TelegramClient()
    result = await client.send_message(chat_id=999, text="hello")

    assert result.message_id == 42
    assert result.chat_id == 999
    assert result.sent_at is not None
    mock_bot.send_message.assert_awaited_once_with(
        chat_id=999, text="hello", parse_mode="HTML", reply_markup=None
    )


# ---- send_message — error mapping ----------------------------------------


async def test_send_message_403_raises_blocked(mock_bot, with_token):
    mock_bot.send_message.side_effect = Forbidden("Forbidden: bot was blocked by the user")

    client = TelegramClient()
    with pytest.raises(TelegramBlockedError):
        await client.send_message(chat_id=999, text="hello")


async def test_send_message_400_chat_not_found_raises_specific(mock_bot, with_token):
    mock_bot.send_message.side_effect = BadRequest("Bad Request: chat not found")

    client = TelegramClient()
    with pytest.raises(TelegramChatNotFoundError):
        await client.send_message(chat_id=999, text="hello")


async def test_send_message_400_other_raises_generic(mock_bot, with_token):
    """Non-chat-not-found BadRequest must NOT be mistaken for the deactivation
    case — it's a transient error worth logging but not deactivating channels."""
    mock_bot.send_message.side_effect = BadRequest("Bad Request: message is too long")

    client = TelegramClient()
    with pytest.raises(TelegramError) as exc_info:
        await client.send_message(chat_id=999, text="hello")
    # Specifically NOT the chat-not-found subclass
    assert not isinstance(exc_info.value, TelegramChatNotFoundError)
    assert not isinstance(exc_info.value, TelegramBlockedError)


async def test_send_message_network_error_raises_generic(mock_bot, with_token):
    mock_bot.send_message.side_effect = NetworkError("connection lost")

    client = TelegramClient()
    with pytest.raises(TelegramError):
        await client.send_message(chat_id=999, text="hello")


# ---- send_message — no token ---------------------------------------------


async def test_send_message_no_token_raises(monkeypatch):
    """Per asymmetric no-op policy: send raises loudly; never silently drops."""
    monkeypatch.setattr(settings, "telegram_bot_token", "")
    client = TelegramClient()
    with pytest.raises(TelegramError, match="not configured"):
        await client.send_message(chat_id=999, text="hello")


# ---- set_webhook — always pushes config (no skip-on-match) ----------------


async def test_set_webhook_always_calls_telegram(mock_bot, with_token):
    """Always-set policy — every call hits Telegram. No getWebhookInfo
    pre-check. Telegram is idempotent on their side; we don't try to be
    clever about it. Critically, this means secret rotation works without
    any URL change — see telegram.py docstring."""
    client = TelegramClient()
    called = await client.set_webhook(
        url="https://app.example.com/api/telegram/webhook/abc",
        secret_token="s3cr3t",
        allowed_updates=["message", "my_chat_member"],
    )

    assert called is True
    mock_bot.set_webhook.assert_awaited_once_with(
        url="https://app.example.com/api/telegram/webhook/abc",
        secret_token="s3cr3t",
        allowed_updates=["message", "my_chat_member"],
    )
    # Pre-check is gone — getWebhookInfo MUST NOT be called by set_webhook.
    mock_bot.get_webhook_info.assert_not_awaited()


async def test_set_webhook_secret_rotation_works(mock_bot, with_token):
    """Pin the contract — calling with same URL but new secret pushes the new
    secret. This is the bug the previous "idempotent" design had: it saw the
    URL match and skipped, leaving the old secret in place at Telegram."""
    client = TelegramClient()

    await client.set_webhook(url="https://x/wh", secret_token="old", allowed_updates=["message"])
    await client.set_webhook(url="https://x/wh", secret_token="NEW", allowed_updates=["message"])

    # Two calls, second one carries the new secret. Telegram receives both.
    assert mock_bot.set_webhook.await_count == 2
    second_call_kwargs = mock_bot.set_webhook.await_args_list[1].kwargs
    assert second_call_kwargs["secret_token"] == "NEW"


# ---- Webhook ops — no-token semantics ------------------------------------


async def test_set_webhook_no_token_is_noop(monkeypatch):
    monkeypatch.setattr(settings, "telegram_bot_token", "")
    client = TelegramClient()
    called = await client.set_webhook(url="x", secret_token="y", allowed_updates=[])
    assert called is False


async def test_get_webhook_info_no_token_returns_empty_dict(monkeypatch):
    monkeypatch.setattr(settings, "telegram_bot_token", "")
    client = TelegramClient()
    assert await client.get_webhook_info() == {}


async def test_delete_webhook_no_token_is_noop(monkeypatch):
    monkeypatch.setattr(settings, "telegram_bot_token", "")
    client = TelegramClient()
    # No raise = no-op success.
    await client.delete_webhook()


# ---- Bot caching ---------------------------------------------------------


def test_bot_cache_invalidates_on_token_change(monkeypatch):
    """Without invalidation, monkeypatching telegram_bot_token wouldn't take
    effect — tests would silently use the old token. Pin this behaviour."""
    monkeypatch.setattr(settings, "telegram_bot_token", "token-A")
    client = TelegramClient()
    bot_a = client._bot()
    assert bot_a.token == "token-A"

    monkeypatch.setattr(settings, "telegram_bot_token", "token-B")
    bot_b = client._bot()
    assert bot_b.token == "token-B"
    assert bot_a is not bot_b
