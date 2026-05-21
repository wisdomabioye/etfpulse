"""PR D.5.2 — `/execute` bot command tests.

The handler is stateless (no DB read/write — no patch_session needed).
We assert three branches by mocking `update.effective_message.reply_html`
+ `reply_markup` and the chat type:

  1. DM + frontend_url set → InlineKeyboardMarkup with WebAppInfo button,
     intro text rendered, URL = `${frontend_url}/execute`.
  2. Group invocation → text-only "DM only" reply, NO web_app button.
  3. DM + frontend_url empty → text-only "not configured" reply.

Plus a smoke test asserting the handler is wired into the command
registry and shows up via `/help`.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import Chat, InlineKeyboardMarkup

from etfpulse.bot.commands import COMMAND_SPECS
from etfpulse.bot.handlers.execute import cmd_execute
from etfpulse.config import settings


def _dm_update() -> MagicMock:
    update = MagicMock()
    update.effective_chat.id = 42
    update.effective_chat.type = Chat.PRIVATE
    update.effective_user.id = 42
    update.effective_user.language_code = "en"
    update.effective_message.reply_html = AsyncMock()
    return update


def _group_update() -> MagicMock:
    update = MagicMock()
    update.effective_chat.id = -100500
    update.effective_chat.type = Chat.GROUP
    update.effective_user.id = 999
    update.effective_user.language_code = "en"
    update.effective_message.reply_html = AsyncMock()
    return update


def _ctx() -> MagicMock:
    return MagicMock()


@pytest.fixture(autouse=True)
def _frontend_url(monkeypatch):
    """Default to a valid HTTPS frontend_url for the happy path. Per-test
    monkeypatch can override to test the empty/HTTP branches."""
    monkeypatch.setattr(settings, "frontend_url", "https://etfpulse.example.com")


async def test_dm_shows_webapp_button_with_intro_text():
    update = _dm_update()
    await cmd_execute(update, _ctx())
    update.effective_message.reply_html.assert_awaited_once()
    call = update.effective_message.reply_html.await_args
    body = call.args[0]
    markup = call.kwargs["reply_markup"]
    assert isinstance(markup, InlineKeyboardMarkup)
    # Single row, single button, with WebAppInfo URL = frontend_url + /execute.
    rows = markup.inline_keyboard
    assert len(rows) == 1
    assert len(rows[0]) == 1
    btn = rows[0][0]
    assert btn.web_app is not None
    assert btn.web_app.url == "https://etfpulse.example.com/execute"
    # Intro mentions wallet signing — distinctive substring locks the
    # i18n copy from drifting silently.
    assert "wallet" in body.lower()


async def test_dm_trailing_slash_normalized(monkeypatch):
    """frontend_url with trailing slash → rstrip prevents `//execute`.

    Use monkeypatch (NOT direct settings mutation) so the autouse
    fixture's revert tracking stays consistent with the rest of the
    file. Direct mutation bypasses monkeypatch's bookkeeping and is
    brittle to fixture-ordering changes."""
    monkeypatch.setattr(settings, "frontend_url", "https://etfpulse.example.com/")
    update = _dm_update()
    await cmd_execute(update, _ctx())
    markup = update.effective_message.reply_html.await_args.kwargs["reply_markup"]
    url = markup.inline_keyboard[0][0].web_app.url
    assert url == "https://etfpulse.example.com/execute"
    assert "//execute" not in url


async def test_group_replies_with_dm_only_no_button():
    update = _group_update()
    await cmd_execute(update, _ctx())
    update.effective_message.reply_html.assert_awaited_once()
    call = update.effective_message.reply_html.await_args
    body = call.args[0]
    # NO reply_markup → no WebApp button (group launches lack stable user identity).
    assert "reply_markup" not in call.kwargs or call.kwargs.get("reply_markup") is None
    assert "DM" in body or "direct" in body.lower()


async def test_supergroup_also_rejected():
    """Supergroup is functionally a group; same restriction applies."""
    update = _group_update()
    update.effective_chat.type = Chat.SUPERGROUP
    await cmd_execute(update, _ctx())
    body = update.effective_message.reply_html.await_args.args[0]
    assert "DM" in body or "direct" in body.lower()


async def test_dm_with_empty_frontend_url_shows_misconfig(monkeypatch):
    monkeypatch.setattr(settings, "frontend_url", "")
    update = _dm_update()
    await cmd_execute(update, _ctx())
    update.effective_message.reply_html.assert_awaited_once()
    body = update.effective_message.reply_html.await_args.args[0]
    # No button, message references the missing env var.
    assert "FRONTEND_URL" in body or "not configured" in body.lower()
    call = update.effective_message.reply_html.await_args
    assert "reply_markup" not in call.kwargs or call.kwargs.get("reply_markup") is None


async def test_dm_http_url_falls_back_to_misconfig_message(monkeypatch):
    """Telegram clients reject non-HTTPS WebAppInfo URLs at click time
    (`WebAppInfo(url=...)` does NOT validate the scheme at construction).
    The handler MUST gate explicitly — otherwise the bot ships a
    button that Telegram refuses to open with no operator-visible
    error. Same fallback as the empty-FRONTEND_URL branch."""
    monkeypatch.setattr(settings, "frontend_url", "http://etfpulse.example.com")
    update = _dm_update()
    await cmd_execute(update, _ctx())
    update.effective_message.reply_html.assert_awaited_once()
    body = update.effective_message.reply_html.await_args.args[0]
    assert "FRONTEND_URL" in body or "not configured" in body.lower()
    call = update.effective_message.reply_html.await_args
    assert "reply_markup" not in call.kwargs or call.kwargs.get("reply_markup") is None


async def test_spanish_locale():
    update = _dm_update()
    update.effective_user.language_code = "es"
    await cmd_execute(update, _ctx())
    body = update.effective_message.reply_html.await_args.args[0]
    # Spanish intro mentions "billetera" (wallet)
    assert "billetera" in body.lower()


# ---------------------------------------------------------------------------
# Registry wiring smoke
# ---------------------------------------------------------------------------


def test_execute_is_in_command_specs():
    names = {spec.name for spec in COMMAND_SPECS}
    assert "execute" in names


def test_execute_spec_is_advertised():
    """`/execute` shows up in the slash-menu + /help. If you ever need
    to hide it (e.g., temporarily disable), flip `advertised=False`."""
    spec = next(s for s in COMMAND_SPECS if s.name == "execute")
    assert spec.advertised is True
    assert spec.description_key == "cmd.execute.desc"


def test_execute_handler_is_registered():
    """Pin the wiring so a future drop of the import in __init__.py
    fails this test instead of a silent boot-time RuntimeError."""
    from etfpulse.bot.handlers import _HANDLERS

    assert "execute" in _HANDLERS
    assert _HANDLERS["execute"] is cmd_execute
