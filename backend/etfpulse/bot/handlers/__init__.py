"""Bot command handlers registration point.

Anti-drift rule (D16): handler modules live in `bot/handlers/*.py` and are
wired into the PTB `Application` exclusively via `register_handlers()` below.
Never call `application.add_handler()` from outside this module — keeps the
registration surface greppable and auditable.

Each handler module exports one or two `cmd_*` functions. This module's job
is the composition: listing every command name + handler pair.
"""

from __future__ import annotations

from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
)

from etfpulse.bot.handlers.callbacks import handle_callback
from etfpulse.bot.handlers.help import cmd_help
from etfpulse.bot.handlers.membership import handle_my_chat_member
from etfpulse.bot.handlers.prefs import cmd_prefs
from etfpulse.bot.handlers.start import cmd_start
from etfpulse.bot.handlers.subscribe import cmd_subscribe, cmd_unsubscribe
from etfpulse.bot.handlers.track_record import cmd_performance, cmd_track_record

# Single source of truth — command name → handler. Ordering doesn't matter
# (PTB matches by exact command name), but alphabetical keeps diffs readable.
# `performance` is the design-doc-spec'd alias for `track_record` — same
# handler bound under two command names; PTB dispatches whichever the user typed.
_COMMANDS: list[tuple[str, object]] = [
    ("help", cmd_help),
    ("performance", cmd_performance),
    ("prefs", cmd_prefs),
    ("start", cmd_start),
    ("subscribe", cmd_subscribe),
    ("track_record", cmd_track_record),  # slash-command names can't have dashes
    ("unsubscribe", cmd_unsubscribe),
]


def register_handlers(application: Application) -> None:
    """Wire every bot handler onto the PTB Application.

    Called once from `bot/lifespan.py:start_bot` after `Application.builder()`
    and before `application.initialize()`.
    """
    for name, handler in _COMMANDS:
        application.add_handler(CommandHandler(name, handler))  # type: ignore[arg-type]

    # my_chat_member — auto-register groups (issue #35). The MY_CHAT_MEMBER
    # filter narrows to updates about THIS bot's membership; the sibling
    # CHAT_MEMBER fires for other users joining/leaving and needs admin
    # rights to receive — which we don't have or want. Lives outside
    # `_COMMANDS` because it's not a CommandHandler.
    application.add_handler(
        ChatMemberHandler(handle_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER)
    )

    # Inline-keyboard callbacks (issue #38). One handler dispatches all
    # callback_data via the prefix routing in `callbacks.handle_callback`;
    # adding a new keyboard surface = a new prefix branch, not a new
    # registered handler. `pattern=None` (default) catches every
    # CallbackQuery so we keep introspection of "what data did we get?"
    # in code rather than in PTB's regex registry.
    application.add_handler(CallbackQueryHandler(handle_callback))


__all__ = ["register_handlers"]
