"""/help — render the advertised command list, i18n-aware.

Body is composed from two i18n surfaces (`help.header` + `render_command_list`)
so /help, /start welcomes, and the Telegram client slash-menu all derive
from the same `bot/commands.py:COMMAND_SPECS` source. Adding a command =
one entry in COMMAND_SPECS + its `cmd.<name>.desc` translation key. No
edits here.
"""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from etfpulse.bot.i18n import render_command_list, resolve_lang, t


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message is None:
        return
    lang = resolve_lang(update)
    text = f"{t('help.header', lang=lang)}\n\n{render_command_list(lang)}"
    await update.effective_message.reply_html(text)


__all__ = ["cmd_help"]
