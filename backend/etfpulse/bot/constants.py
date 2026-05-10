"""Shared bot-domain constants.

Single source of truth for values that BOTH `bot/lifespan.py` (initial
webhook registration) AND `api/routes/admin.py` (hot-rotation endpoint)
need to agree on. Putting them here eliminates the silent-drift class
of bug where the two surfaces register Telegram with DIFFERENT
configurations:

- `ALLOWED_UPDATES` previously lived as a private copy in each module.
  When `callback_query` was added to lifespan for issue #38, the admin
  rotation endpoint's copy stayed at the old 2-element list — so a
  hot-rotation would have silently downgraded the webhook to exclude
  callbacks until the next container restart.
- `VALID_ASSETS` was duplicated as `_VALID_ASSETS` (validator) and
  `_PREFS_ASSETS` (keyboard). Adding a new asset meant updating both
  in lockstep; the comment "match the other" is fragile-by-convention.

CLAUDE.md's domain layering rule blocks `bot → api` imports but not
`api → bot`. The admin route importing from here is allowed.
"""

from __future__ import annotations

from typing import Final

from etfpulse.constants import SUPPORTED_ASSETS

# Telegram update types the bot is registered to receive. Passed to
# `set_webhook(allowed_updates=...)` at both initial registration and
# hot rotation. Adding a new update type = appending here + writing a
# handler that consumes it. Removing one cleanly requires both the
# lifespan registration AND any rotation in-flight to be on the new
# value, otherwise the rotation would re-add the old type.
ALLOWED_UPDATES: Final[list[str]] = [
    "message",  # commands + plain text (handled by CommandHandler set)
    "my_chat_member",  # bot added/removed/promoted in a chat — issue #35
    "callback_query",  # inline keyboard taps — issue #38
]

# Bot-domain alias for the package-level `SUPPORTED_ASSETS`. Same value,
# bot-domain name — keeps `/prefs` validator and keyboard imports
# expressive ("VALID_ASSETS" reads as "what /prefs accepts") while the
# global truth still lives in `etfpulse.constants`. Adding an asset =
# editing that one constant; tests on detectors, keyboard, validator
# pick it up automatically.
VALID_ASSETS: Final[tuple[str, ...]] = SUPPORTED_ASSETS
