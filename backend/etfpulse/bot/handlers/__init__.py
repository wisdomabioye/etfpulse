"""Bot command handlers registration point.

Anti-drift rule (D16): handler modules live in `bot/handlers/*.py` and are
wired into the PTB `Application` exclusively via `register_handlers()` below.
Never call `application.add_handler()` from outside this module — keeps the
registration surface greppable and auditable.

Source of truth for which commands exist: `bot/commands.py:COMMAND_SPECS`.
This module's job is the composition: mapping each spec.name to the
handler callable it dispatches to. `_HANDLERS` MUST cover every spec.name
(including unadvertised aliases) — the runtime invariant in
`register_handlers` raises if a spec is missing a handler, and the test
`test_every_command_spec_has_handler` pins that contract at PR-review time.

Adding a command: extend `COMMAND_SPECS` AND map `_HANDLERS[name]` here.
Adding an alias: extend `COMMAND_SPECS` with `advertised=False` AND map
`_HANDLERS[alias] = same_handler_as_primary`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
)

from etfpulse.bot.commands import COMMAND_SPECS
from etfpulse.bot.handlers.callbacks import handle_callback
from etfpulse.bot.handlers.execute import cmd_execute
from etfpulse.bot.handlers.help import cmd_help
from etfpulse.bot.handlers.membership import handle_my_chat_member
from etfpulse.bot.handlers.prefs import cmd_prefs
from etfpulse.bot.handlers.start import cmd_start
from etfpulse.bot.handlers.subscribe import cmd_subscribe, cmd_unsubscribe
from etfpulse.bot.handlers.track_record import cmd_performance, cmd_track_record

# Name → handler. Every name in COMMAND_SPECS MUST appear here. The dispatch
# loop in `register_handlers` raises if a spec is missing — caught at boot
# rather than as a silent "Telegram sends /performance, nothing happens".
# Aliases bind the same callable as their primary: `track_record` shares
# `cmd_track_record` with the advertised `performance` entry.
_HANDLERS: dict[str, Callable[..., Any]] = {
    "execute": cmd_execute,
    "help": cmd_help,
    "performance": cmd_performance,
    "prefs": cmd_prefs,
    "start": cmd_start,
    "subscribe": cmd_subscribe,
    "track_record": cmd_track_record,
    "unsubscribe": cmd_unsubscribe,
}


def register_handlers(application: Application) -> None:
    """Wire every bot handler onto the PTB Application.

    Called once from `bot/lifespan.py:start_bot` after `Application.builder()`
    and before `application.initialize()`. Raises `RuntimeError` on registry
    drift (spec without a handler) so a misconfigured deploy fails loud at
    boot instead of silently dropping commands.
    """
    for spec in COMMAND_SPECS:
        handler = _HANDLERS.get(spec.name)
        if handler is None:
            raise RuntimeError(
                f"Command registry drift: COMMAND_SPECS lists {spec.name!r} "
                f"but bot/handlers/__init__.py has no handler in _HANDLERS. "
                f"Add the mapping or remove the spec."
            )
        application.add_handler(CommandHandler(spec.name, handler))

    # my_chat_member — auto-register groups (issue #35). Lives outside the
    # spec table because it's not a CommandHandler. The MY_CHAT_MEMBER filter
    # narrows to updates about THIS bot's membership; the sibling CHAT_MEMBER
    # fires for other users and needs admin rights to receive.
    application.add_handler(
        ChatMemberHandler(handle_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER)
    )

    # Inline-keyboard callbacks (issue #38). One handler dispatches all
    # callback_data via prefix routing in `callbacks.handle_callback`;
    # adding a new keyboard surface = a new prefix branch, not a new
    # registered handler. `pattern=None` (default) catches every
    # CallbackQuery so we keep introspection of "what data did we get?"
    # in code rather than in PTB's regex registry.
    application.add_handler(CallbackQueryHandler(handle_callback))


__all__ = ["register_handlers"]
