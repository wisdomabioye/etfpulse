"""CallbackQuery handler — inline-keyboard taps (issue #38).

Uses the same SAVEPOINT pattern as `test_handlers.py` so the handler's
own `session.commit()` writes to a savepoint that rolls back at test
teardown. The callback dispatcher opens its own `async_session()`, so
that module is added to the patched-session list here.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select
from telegram import Chat, ChatMember

from etfpulse.bot.handlers.callbacks import handle_callback
from etfpulse.bot.handlers.start import cmd_start
from etfpulse.models import NotificationChannel, TelegramGroup, User


@pytest.fixture
def patch_session(monkeypatch, db_session):
    """Mirror the pattern in `test_handlers.py` — both the /start handler
    (used to seed a User row) and the callbacks dispatcher open their own
    session, so both must be patched."""

    @asynccontextmanager
    async def _yielder():
        async with db_session.begin_nested():
            yield db_session

    for mod in (
        "etfpulse.bot.handlers.start",
        "etfpulse.bot.handlers.callbacks",
    ):
        monkeypatch.setattr(f"{mod}.async_session", _yielder)


def _dm_update(
    *, chat_id: int = 42, callback_data: str, language_code: str | None = None
) -> MagicMock:
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_chat.type = Chat.PRIVATE
    update.effective_user.id = chat_id
    update.effective_user.username = "alice"
    # Pin so resolve_lang gets a real string-or-None, not a MagicMock.
    update.effective_user.language_code = language_code
    update.effective_message.reply_html = AsyncMock()
    cb = MagicMock()
    cb.data = callback_data
    cb.from_user = update.effective_user
    cb.answer = AsyncMock()
    cb.edit_message_reply_markup = AsyncMock()
    update.callback_query = cb
    return update


def _group_update(
    *,
    chat_id: int = -100600,
    callback_data: str,
    admin_status: str | None,
    language_code: str | None = None,
) -> MagicMock:
    """Group callback update. `admin_status=None` → get_chat_member raises
    → fails closed → denied."""
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_chat.type = Chat.SUPERGROUP
    update.effective_user.id = 999
    update.effective_user.username = "bob"
    update.effective_user.language_code = language_code
    update.effective_message.reply_html = AsyncMock()
    cb = MagicMock()
    cb.data = callback_data
    cb.from_user = update.effective_user
    cb.answer = AsyncMock()
    cb.edit_message_reply_markup = AsyncMock()
    update.callback_query = cb
    update._admin_status = admin_status  # consumed by _ctx below
    return update


def _ctx(member_status: str | None = ChatMember.ADMINISTRATOR) -> MagicMock:
    """Mock ContextTypes with bot.get_chat_member resolved or raising."""
    from telegram.error import TelegramError

    ctx = MagicMock()
    ctx.args = []
    if member_status is None:
        ctx.bot.get_chat_member = AsyncMock(side_effect=TelegramError("api down"))
    else:
        member = MagicMock()
        member.status = member_status
        ctx.bot.get_chat_member = AsyncMock(return_value=member)
    return ctx


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


class TestRouting:
    async def test_unknown_prefix_silently_answers(self, db_session, patch_session):
        upd = _dm_update(callback_data="unknown_prefix:do_something")
        await handle_callback(upd, _ctx())
        upd.callback_query.answer.assert_awaited_once()

    async def test_noop_silently_answers(self, db_session, patch_session):
        """The inert confidence label callback (prefs:noop) just clears the spinner."""
        await cmd_start(_seed_user_update(100), MagicMock(args=[]))
        upd = _dm_update(chat_id=100, callback_data="prefs:noop")
        await handle_callback(upd, _ctx())
        upd.callback_query.answer.assert_awaited_once()
        # No DB mutation, no edit.
        upd.callback_query.edit_message_reply_markup.assert_not_called()

    async def test_missing_callback_data_returns_silently(self, db_session, patch_session):
        upd = _dm_update(callback_data="prefs:toggle_asset:BTC")
        upd.callback_query.data = None
        await handle_callback(upd, _ctx())
        # No answer called (early return on missing data).
        upd.callback_query.answer.assert_not_called()


def _seed_user_update(chat_id: int) -> MagicMock:
    """Helper to build a DM /start update so the User row exists before
    the callback fires."""
    upd = MagicMock()
    upd.effective_chat.id = chat_id
    upd.effective_chat.type = Chat.PRIVATE
    upd.effective_chat.title = None
    upd.effective_user.id = chat_id
    upd.effective_user.username = "user"
    upd.effective_message.reply_html = AsyncMock()
    return upd


# ---------------------------------------------------------------------------
# DM happy paths
# ---------------------------------------------------------------------------


class TestDMCallbacks:
    async def test_toggle_asset_adds_then_removes(self, db_session, patch_session):
        await cmd_start(_seed_user_update(100), MagicMock(args=[]))
        # Default has BTC + ETH; toggling BTC should remove it.
        upd = _dm_update(chat_id=100, callback_data="prefs:toggle_asset:BTC")
        await handle_callback(upd, _ctx())

        channel = (
            await db_session.execute(
                select(NotificationChannel).where(NotificationChannel.channel_identifier == "100")
            )
        ).scalar_one()
        user = await db_session.get(User, channel.user_id)
        assert user is not None
        assert "BTC" not in user.pref_assets
        assert "ETH" in user.pref_assets

        # Toggling again re-adds it.
        upd2 = _dm_update(chat_id=100, callback_data="prefs:toggle_asset:BTC")
        await handle_callback(upd2, _ctx())
        await db_session.refresh(user)
        assert "BTC" in user.pref_assets

    async def test_conf_inc_bounded_at_10(self, db_session, patch_session):
        await cmd_start(_seed_user_update(101), MagicMock(args=[]))
        channel = (
            await db_session.execute(
                select(NotificationChannel).where(NotificationChannel.channel_identifier == "101")
            )
        ).scalar_one()
        user = await db_session.get(User, channel.user_id)
        assert user is not None
        user.pref_min_confidence = 10
        await db_session.flush()

        await handle_callback(_dm_update(chat_id=101, callback_data="prefs:conf_inc"), _ctx())
        await db_session.refresh(user)
        # Bounded — doesn't go past 10.
        assert user.pref_min_confidence == 10

    async def test_conf_dec_bounded_at_1(self, db_session, patch_session):
        await cmd_start(_seed_user_update(102), MagicMock(args=[]))
        channel = (
            await db_session.execute(
                select(NotificationChannel).where(NotificationChannel.channel_identifier == "102")
            )
        ).scalar_one()
        user = await db_session.get(User, channel.user_id)
        assert user is not None
        user.pref_min_confidence = 1
        await db_session.flush()

        await handle_callback(_dm_update(chat_id=102, callback_data="prefs:conf_dec"), _ctx())
        await db_session.refresh(user)
        assert user.pref_min_confidence == 1

    async def test_callback_toast_translated_when_language_code_set(
        self, db_session, patch_session
    ):
        """Issue #37 — toast strings translate via `resolve_lang` →
        `t(key, lang=...)`. A Spanish-locale tapper hitting the last-asset
        guard sees the Spanish message."""
        await cmd_start(_seed_user_update(106), MagicMock(args=[]))
        channel = (
            await db_session.execute(
                select(NotificationChannel).where(NotificationChannel.channel_identifier == "106")
            )
        ).scalar_one()
        user = await db_session.get(User, channel.user_id)
        assert user is not None
        user.pref_assets = ["BTC"]
        await db_session.flush()

        upd = _dm_update(
            chat_id=106,
            callback_data="prefs:toggle_asset:BTC",
            language_code="es",
        )
        await handle_callback(upd, _ctx())

        args = upd.callback_query.answer.await_args
        assert args.args and "al menos un activo" in args.args[0]

    async def test_toggle_asset_refuses_to_remove_last(self, db_session, patch_session):
        """Empty pref_assets means 'all assets' per fan-out semantics — the
        opposite of what a user toggling off everything intends. Refuse
        the last-asset toggle and emit a toast pointing at pause."""
        await cmd_start(_seed_user_update(104), MagicMock(args=[]))
        channel = (
            await db_session.execute(
                select(NotificationChannel).where(NotificationChannel.channel_identifier == "104")
            )
        ).scalar_one()
        user = await db_session.get(User, channel.user_id)
        assert user is not None
        # Pin a single-asset list so the next toggle would empty it.
        user.pref_assets = ["BTC"]
        await db_session.flush()

        upd = _dm_update(chat_id=104, callback_data="prefs:toggle_asset:BTC")
        await handle_callback(upd, _ctx())

        await db_session.refresh(user)
        # State unchanged.
        assert user.pref_assets == ["BTC"]
        # Toast with the redirect copy.
        upd.callback_query.answer.assert_awaited()
        args = upd.callback_query.answer.await_args
        assert args.args and "Keep at least one asset" in args.args[0]
        # No re-render — the action was a no-op.
        upd.callback_query.edit_message_reply_markup.assert_not_called()

    async def test_successful_mutation_re_renders_keyboard(self, db_session, patch_session):
        """After a state change, edit_message_reply_markup MUST fire so the
        user sees the new keyboard (e.g. flipped ✅ on the toggled asset).
        Without this, the DB updates but the UI looks stale until /prefs
        is re-issued."""
        from telegram import InlineKeyboardMarkup

        await cmd_start(_seed_user_update(105), MagicMock(args=[]))
        upd = _dm_update(chat_id=105, callback_data="prefs:conf_inc")
        await handle_callback(upd, _ctx())

        upd.callback_query.edit_message_reply_markup.assert_awaited_once()
        kwargs = upd.callback_query.edit_message_reply_markup.await_args.kwargs
        # Verify the re-render uses a fresh keyboard (not None).
        assert isinstance(kwargs["reply_markup"], InlineKeyboardMarkup)

    async def test_pause_and_resume_flip_is_active(self, db_session, patch_session):
        await cmd_start(_seed_user_update(103), MagicMock(args=[]))

        await handle_callback(_dm_update(chat_id=103, callback_data="prefs:pause"), _ctx())
        channel = (
            await db_session.execute(
                select(NotificationChannel).where(NotificationChannel.channel_identifier == "103")
            )
        ).scalar_one()
        user = await db_session.get(User, channel.user_id)
        assert user is not None
        assert user.is_active is False

        await handle_callback(_dm_update(chat_id=103, callback_data="prefs:resume"), _ctx())
        await db_session.refresh(user)
        assert user.is_active is True

    async def test_missing_user_returns_friendly_toast(self, db_session, patch_session):
        """Tapping a stale keyboard from before /start: no row exists. Should
        show a toast, not error."""
        upd = _dm_update(chat_id=999, callback_data="prefs:toggle_asset:BTC")
        await handle_callback(upd, _ctx())
        upd.callback_query.answer.assert_awaited()
        # The answer was called with a toast string (not the bare clear-spinner).
        args = upd.callback_query.answer.await_args
        assert args.args and "Start with /start" in args.args[0]


# ---------------------------------------------------------------------------
# Group admin gate
# ---------------------------------------------------------------------------


class TestGroupAdminGate:
    async def _seed_group(self, db_session, chat_id: int) -> TelegramGroup:
        group = TelegramGroup(
            chat_id=chat_id,
            title="t",
            pref_assets=["BTC", "ETH"],
            pref_min_confidence=5,
        )
        db_session.add(group)
        await db_session.flush()
        return group

    async def test_admin_can_apply(self, db_session, patch_session):
        await self._seed_group(db_session, -200100)
        upd = _group_update(
            chat_id=-200100,
            callback_data="prefs:pause",
            admin_status=ChatMember.ADMINISTRATOR,
        )
        await handle_callback(upd, _ctx(ChatMember.ADMINISTRATOR))

        group = (
            await db_session.execute(select(TelegramGroup).where(TelegramGroup.chat_id == -200100))
        ).scalar_one()
        assert group.is_active is False

    async def test_non_admin_denied_with_toast(self, db_session, patch_session):
        await self._seed_group(db_session, -200200)
        upd = _group_update(
            chat_id=-200200,
            callback_data="prefs:pause",
            admin_status=ChatMember.MEMBER,
        )
        await handle_callback(upd, _ctx(ChatMember.MEMBER))

        upd.callback_query.answer.assert_awaited()
        args = upd.callback_query.answer.await_args
        assert args.args and "Only group admins" in args.args[0]

        # State unchanged.
        group = (
            await db_session.execute(select(TelegramGroup).where(TelegramGroup.chat_id == -200200))
        ).scalar_one()
        assert group.is_active is True

    async def test_api_failure_fails_closed(self, db_session, patch_session):
        """get_chat_member raising → treat as non-admin → denied."""
        await self._seed_group(db_session, -200300)
        upd = _group_update(
            chat_id=-200300,
            callback_data="prefs:resume",
            admin_status=None,
        )
        await handle_callback(upd, _ctx(None))

        group = (
            await db_session.execute(select(TelegramGroup).where(TelegramGroup.chat_id == -200300))
        ).scalar_one()
        # is_active default True, unchanged.
        assert group.is_active is True
