"""Command registry — single source of truth for bot command metadata.

Drives three surfaces from one list:

  1. **Handler registration** (`bot/handlers/__init__.py`) — every spec.name
     resolves to a handler in `_HANDLERS`; a startup assertion catches drift.
  2. **/help and /start welcome rendering** (`bot/i18n.py:render_command_list`)
     — only `advertised=True` entries appear in user-facing copy.
  3. **Telegram client slash-menu** (`bot/lifespan.py:start_bot`) — pushes the
     advertised set via `bot.set_my_commands` so the typing experience in
     Telegram clients matches what /help displays.

Adding a command:

    1. Create handler module in `bot/handlers/<name>.py` exporting `cmd_<name>`.
    2. Import + register in `_HANDLERS` (bot/handlers/__init__.py).
    3. Add a `CommandSpec` entry below.
    4. Add `cmd.<name>.desc` translation key in `bot/i18n.py` (English mandatory).

Aliases — register an unadvertised spec whose `description_key` and handler
match an advertised one. Power users typing the alias still hit the same
code path; the alias is excluded from /help, welcomes, and the slash-menu.

`track_record` exists as an unadvertised alias for `performance`: Telegram
bot command names are restricted to `[a-z0-9_]+` (hyphens illegal), so
`/track-record` is unreachable as a command — every user-facing copy now
advertises `/performance` instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True, slots=True)
class CommandSpec:
    """One row in the command registry.

    `name` MUST be Telegram-legal (`[a-z0-9_]+`) — `set_my_commands` rejects
    anything else with a 400 at startup. The runtime test
    `test_command_specs_have_telegram_legal_names` enforces this.

    `description_key` is the i18n lookup; the resolved string renders next
    to the command in /help, in the start-welcome list, and in the Telegram
    slash-menu. Shared keys are fine (aliases reuse their primary's key).
    """

    name: str
    description_key: str
    advertised: bool = True


# Tuple ordering = display order in /help, welcomes, and the slash-menu.
# `start` first so newcomers see the entry point at the top.
COMMAND_SPECS: Final[tuple[CommandSpec, ...]] = (
    CommandSpec("start", "cmd.start.desc"),
    CommandSpec("prefs", "cmd.prefs.desc"),
    CommandSpec("subscribe", "cmd.subscribe.desc"),
    CommandSpec("unsubscribe", "cmd.unsubscribe.desc"),
    CommandSpec("performance", "cmd.performance.desc"),
    CommandSpec("help", "cmd.help.desc"),
    # Unadvertised alias of `performance`. Reuses the same description key
    # because if it ever appears anywhere (e.g., a future debug command
    # that dumps the full registry), users should read the same sentence.
    CommandSpec("track_record", "cmd.performance.desc", advertised=False),
)


__all__ = ["COMMAND_SPECS", "CommandSpec"]
