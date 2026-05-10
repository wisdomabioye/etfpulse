"""/prefs — set delivery preferences.

Grammar (minimal on purpose — avoids parser complexity):
    /prefs assets BTC,ETH
    /prefs confidence 7
    /prefs                → shows current prefs

Invalid input responds with a help message rather than erroring silently.
"""

from __future__ import annotations

import structlog
from telegram import InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from etfpulse.bot.handlers._common import (
    get_or_create_target,
    is_group_admin,
    parse_asset_list,
    parse_confidence,
)
from etfpulse.bot.keyboards import build_prefs_keyboard
from etfpulse.db import async_session

log = structlog.get_logger()


_USAGE = (
    "<b>Usage:</b>\n"
    "• <code>/prefs</code> — show current settings\n"
    "• <code>/prefs assets BTC,ETH</code>\n"
    "• <code>/prefs confidence 7</code>"
)

# Surfaced to the user when a group member without admin powers tries to
# change settings. Reading current prefs (no-args /prefs) is still allowed
# for everyone — it's information, not mutation.
_NOT_ADMIN_MSG = (
    "❌ Only group admins can change preferences.\n"
    "You can still run <code>/prefs</code> (no args) to view current settings."
)


async def cmd_prefs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []

    # Issue #39 — gate group mutations behind admin status. The check
    # runs BEFORE opening a DB session so a non-admin attempt doesn't
    # create the TelegramGroup row as a side effect (`get_or_create_target`
    # would auto-create on first /prefs from a new group). Reading
    # current prefs (no args) is allowed for everyone — the no-args
    # branch falls through below.
    if args and not await is_group_admin(update, context):
        await _reply(update, _NOT_ADMIN_MSG)
        log.info(
            "bot_prefs_denied_non_admin",
            chat_id=update.effective_chat.id if update.effective_chat else None,
            user_id=update.effective_user.id if update.effective_user else None,
        )
        return

    async with async_session() as session:
        target = await get_or_create_target(session, update)

        if not args:
            await session.commit()  # ensure any get_or_create writes are saved
            # No-args path also attaches the inline quick-toggle keyboard
            # (issue #38). Subcommand paths still use plain text replies
            # — the keyboard is for "show me + let me tweak" UX, not for
            # confirming a successful explicit /prefs assets / /prefs
            # confidence command.
            await _reply(
                update,
                _format_current_prefs(target.obj),
                reply_markup=build_prefs_keyboard(target.obj),
            )
            return

        sub = args[0].lower()
        if sub == "assets":
            if len(args) < 2:
                await _reply(update, _USAGE)
                return
            try:
                assets = parse_asset_list(args[1])
            except ValueError as exc:
                await _reply(update, f"❌ {exc}")
                return
            target.obj.pref_assets = assets

        elif sub == "confidence":
            if len(args) < 2:
                await _reply(update, _USAGE)
                return
            try:
                conf = parse_confidence(args[1])
            except ValueError as exc:
                await _reply(update, f"❌ {exc}")
                return
            target.obj.pref_min_confidence = conf

        else:
            await _reply(update, _USAGE)
            return

        await session.commit()
        log.info(
            "bot_prefs_updated",
            kind=target.kind,
            id=target.obj.id,
            field=sub,
        )
        await _reply(
            update,
            f"✅ Updated <code>{sub}</code>.\n\n" + _format_current_prefs(target.obj),
        )


def _format_current_prefs(obj) -> str:
    """Renders the target's current pref_assets + pref_min_confidence."""
    assets = ", ".join(obj.pref_assets) if obj.pref_assets else "(none — all assets)"
    paused = " (paused)" if getattr(obj, "pref_paused", False) else ""
    active = "yes" if getattr(obj, "is_active", True) else "no"
    return (
        "<b>Current preferences:</b>\n"
        f"• Assets: <code>{assets}</code>\n"
        f"• Min confidence: <code>{obj.pref_min_confidence}</code>\n"
        f"• Active: <code>{active}</code>{paused}"
    )


async def _reply(
    update: Update,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Send an HTML reply, optionally with an inline keyboard."""
    if update.effective_message is None:
        return
    await update.effective_message.reply_html(text, reply_markup=reply_markup)


__all__ = ["cmd_prefs"]
