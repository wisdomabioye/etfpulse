"""CallbackQuery handler — receives taps from inline keyboards (issue #38).

Routes on the `<prefix>:<action>[:<arg>]` callback_data shape produced
by `bot/keyboards.py`. Today only the `prefs:` family is wired; future
keyboards add new prefixes and a new branch here.

D17 contract: each branch opens its own `async_session()` and commits
before editing the message. Same handler-isolation invariant as the
command handlers — keeps transaction boundaries explicit.

Group admin gate: callback mutations from a group are subject to the
same admin check as `/prefs` itself (issue #39). The check uses the
ORIGINATING user of the callback (`update.callback_query.from_user`),
not the message author, so a non-admin tapping a button in a group is
denied even if the original /prefs was issued by an admin.
"""

from __future__ import annotations

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from telegram import Chat, Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from etfpulse.bot.handlers._common import is_group_admin
from etfpulse.bot.i18n import resolve_lang, t
from etfpulse.bot.keyboards import PREFS_PREFIX, build_prefs_keyboard
from etfpulse.db import async_session
from etfpulse.models import (
    ChannelType,
    NotificationChannel,
    TelegramGroup,
    User,
)

log = structlog.get_logger()

# Bounds for the confidence ± buttons — match `parse_confidence` in
# `_common.py` and the DB CHECK constraint on `pref_min_confidence`.
_CONF_MIN = 1
_CONF_MAX = 10

# Translation keys for callback-toast strings. answer_callback_query
# strings are capped at 200 chars per Telegram; all translations stay
# well under. Keys resolved via `bot.i18n.t(key, lang=...)`.
_NOT_ADMIN_KEY = "callback.not_admin"
_LAST_ASSET_KEY = "callback.last_asset"
_START_FIRST_KEY = "callback.start_first"


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Top-level dispatcher. Routes on the prefix before the first colon.

    Always answers the callback query to stop the spinner on the user's
    client. On unknown prefixes / missing data we still answer (silently)
    so the UI doesn't hang.
    """
    cb = update.callback_query
    if cb is None or cb.data is None:
        return

    prefix, _, rest = cb.data.partition(":")
    if prefix == PREFS_PREFIX:
        await _handle_prefs_callback(update, context, rest)
        return

    # Unknown prefix — silent answer so the user's client stops spinning.
    log.info("callback_unknown_prefix", data=cb.data)
    await cb.answer()


async def _handle_prefs_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE, rest: str
) -> None:
    cb = update.callback_query
    if cb is None:
        return  # already filtered by handle_callback; kept for the type checker.

    chat = update.effective_chat
    if chat is None:
        await cb.answer()
        return

    lang = resolve_lang(update)

    # Inert label tap — show nothing, just clear the spinner.
    if rest == "noop":
        await cb.answer()
        return

    # Group admin gate (issue #39 parallel). DM callbacks pass through.
    if chat.type in (Chat.GROUP, Chat.SUPERGROUP):
        if not await is_group_admin(update, context):
            await cb.answer(t(_NOT_ADMIN_KEY, lang=lang), show_alert=False)
            log.info(
                "callback_prefs_denied_non_admin",
                chat_id=chat.id,
                user_id=update.effective_user.id if update.effective_user else None,
            )
            return

    async with async_session() as session:
        target = await _resolve_target(session, update)
        if target is None:
            # No row yet — user tapped a stale keyboard from before /start.
            await cb.answer(t(_START_FIRST_KEY, lang=lang), show_alert=False)
            return

        action, _, arg = rest.partition(":")
        if action == "toggle_asset":
            # Refuse to remove the last asset — empty `pref_assets` means
            # "all assets" per the delivery fan-out semantics, which is
            # the OPPOSITE of what a user toggling off everything intends.
            # Direct them at /unsubscribe (or the ⏸ Pause button) for the
            # "stop notifying me" path.
            if not _toggle_asset(target, arg):
                await cb.answer(t(_LAST_ASSET_KEY, lang=lang), show_alert=False)
                return
        elif action == "conf_dec":
            target.pref_min_confidence = max(_CONF_MIN, target.pref_min_confidence - 1)
        elif action == "conf_inc":
            target.pref_min_confidence = min(_CONF_MAX, target.pref_min_confidence + 1)
        elif action == "pause":
            target.is_active = False
        elif action == "resume":
            target.is_active = True
        else:
            log.info("callback_prefs_unknown_action", action=action)
            await cb.answer()
            return

        await session.commit()
        log.info(
            "callback_prefs_applied",
            action=action,
            chat_id=chat.id,
        )

        # Re-render the keyboard so the toggle/value reflects the new
        # state immediately. answer_callback_query also resolves the
        # spinner; edit_message_reply_markup is what visually updates.
        await cb.answer()
        try:
            await cb.edit_message_reply_markup(reply_markup=build_prefs_keyboard(target))
        except TelegramError as exc:
            # Edit can fail for benign reasons (message too old, identical
            # reply_markup). The state IS committed; just log and move on.
            log.info("callback_prefs_edit_failed", error=str(exc))


def _toggle_asset(target: User | TelegramGroup, asset: str) -> bool:
    """Add or remove `asset` from `target.pref_assets`.

    Returns True on success, False when the caller asked to remove the
    LAST asset — refusing keeps the user out of the "I removed everything
    so I get nothing" trap that's actually "I get everything" per the
    `delivery._match_users` empty-list semantics. The caller surfaces a
    toast on False; no DB write happens in that case.

    The valid-asset check is done implicitly by the keyboard (we only
    render buttons for `_PREFS_ASSETS`); a hand-crafted payload with
    `SOL` would just go into the list and then never match any signal.
    Documented limitation.
    """
    current = list(target.pref_assets or [])
    if asset in current:
        if len(current) == 1:
            # Last-asset guard — caller emits the toast.
            return False
        target.pref_assets = [a for a in current if a != asset]
    else:
        target.pref_assets = current + [asset]
    return True


async def _resolve_target(session: AsyncSession, update: Update) -> User | TelegramGroup | None:
    """Find the User (DM) or TelegramGroup (group) the callback applies to.

    Unlike `get_or_create_target` we do NOT create on miss — the inline
    keyboard came from a prior /prefs (or signal alert in future), so a
    missing row means the user has tapped a stale keyboard or never
    completed /start. Returning None lets the caller emit a friendly toast.
    """
    chat = update.effective_chat
    if chat is None:
        return None

    if chat.type == Chat.PRIVATE:
        channel_result = await session.execute(
            select(NotificationChannel).where(
                NotificationChannel.channel_type == ChannelType.TELEGRAM.value,
                NotificationChannel.channel_identifier == str(chat.id),
            )
        )
        channel = channel_result.scalar_one_or_none()
        if channel is None:
            return None
        return await session.get(User, channel.user_id)

    group_result = await session.execute(
        select(TelegramGroup).where(TelegramGroup.chat_id == chat.id)
    )
    return group_result.scalar_one_or_none()


__all__ = ["handle_callback"]
