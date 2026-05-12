"""Command registry invariants — pin the drift-detection contract.

These tests catch the failure modes that motivated `bot/commands.py`:
  - A command appears in `COMMAND_SPECS` but has no handler wired up.
  - A handler exists in `_HANDLERS` but has no spec describing it.
  - A spec advertises a command Telegram cannot dispatch
    (`set_my_commands` rejects names with characters outside `[a-z0-9_]`).
  - An advertised description has no English translation key
    (`render_command_list` would KeyError on the missing key).
  - The `/track-record` hyphen drift slipped back into COMMAND_SPECS.

The whole point of the registry is that adding a new command in one place
shouldn't require remembering to update three. These tests are the
enforcement layer for that promise.
"""

from __future__ import annotations

import dataclasses
import re

import pytest

from etfpulse.bot.commands import COMMAND_SPECS, CommandSpec
from etfpulse.bot.handlers import _HANDLERS
from etfpulse.bot.i18n import _TRANSLATIONS, DEFAULT_LANG

_TELEGRAM_LEGAL_NAME = re.compile(r"^[a-z0-9_]+$")


class TestRegistryWiring:
    def test_every_spec_has_handler(self):
        """A spec without a `_HANDLERS` mapping would raise at boot
        (`register_handlers`). Surfacing it here means PR review catches
        the drift before it reaches a deploy."""
        missing = [spec.name for spec in COMMAND_SPECS if spec.name not in _HANDLERS]
        assert not missing, (
            f"COMMAND_SPECS lists {missing} but bot/handlers/__init__.py "
            f"has no entry in _HANDLERS for them."
        )

    def test_every_handler_has_spec(self):
        """`_HANDLERS` entries without a `COMMAND_SPECS` row are orphaned —
        registered with PTB but invisible to /help, welcomes, and the
        slash-menu. Usually means a spec was deleted but the handler
        mapping wasn't cleaned up."""
        spec_names = {spec.name for spec in COMMAND_SPECS}
        orphans = sorted(set(_HANDLERS) - spec_names)
        assert not orphans, (
            f"_HANDLERS has handlers for {orphans} but COMMAND_SPECS doesn't "
            f"list them. Add a spec (advertised=False for aliases) or "
            f"remove the handler mapping."
        )


class TestRegistryContract:
    def test_names_are_telegram_legal(self):
        """`set_my_commands` rejects names outside `[a-z0-9_]+` with a 400.
        Pin this so a future spec like `track-record` doesn't slip in and
        crash bot startup."""
        bad = [spec.name for spec in COMMAND_SPECS if not _TELEGRAM_LEGAL_NAME.match(spec.name)]
        assert not bad, (
            f"Telegram bot command names must match [a-z0-9_]+; these violate the spec: {bad}"
        )

    def test_description_keys_exist_in_english(self):
        """Every spec.description_key must resolve in English — that's the
        fallback root for partial translations. A missing key would mean
        `render_command_list` KeyErrors AND `set_my_commands` ships an
        empty description (or, worse, raises during boot)."""
        english = _TRANSLATIONS[DEFAULT_LANG]
        missing = sorted({spec.description_key for spec in COMMAND_SPECS} - english.keys())
        assert not missing, (
            f"description_keys without English translation: {missing}. "
            f"Add them to _TRANSLATIONS['en'] in bot/i18n.py."
        )

    def test_track_record_hyphen_is_not_a_command(self):
        """Regression guard. `/track-record` was advertised in /help for
        months even though Telegram clients couldn't dispatch it
        (hyphens illegal). Pin that no spec ever brings the hyphen form back.
        """
        assert not any("-" in spec.name for spec in COMMAND_SPECS), (
            "Telegram bot command names cannot contain hyphens. "
            "Use underscores (and keep aliases unadvertised)."
        )

    def test_performance_is_advertised_track_record_is_alias(self):
        """The hyphen-fix is a load-bearing UX decision: users see
        `/performance` (Telegram-legal AND short), `/track_record` is an
        invisible alias for power users who guess the underscore form.
        Lock the asymmetry in so a future cleanup doesn't accidentally
        flip which one is advertised."""
        by_name = {spec.name: spec for spec in COMMAND_SPECS}
        assert by_name["performance"].advertised is True
        # If `track_record` ever gets removed entirely that's fine — drop
        # this clause. While it exists, it MUST be the unadvertised side.
        if "track_record" in by_name:
            assert by_name["track_record"].advertised is False


class TestCommandSpec:
    def test_frozen_and_hashable(self):
        """`@dataclass(frozen=True, slots=True)` — assignment raises and
        instances are usable as dict keys / in sets. Pinned so a refactor
        toward mutability fails immediately, which would otherwise let
        runtime code accidentally rewrite COMMAND_SPECS in place."""
        spec = CommandSpec("smoke", "cmd.smoke.desc")
        with pytest.raises(dataclasses.FrozenInstanceError):
            spec.name = "other"  # type: ignore[misc]
        # Hashable → usable in sets / dict keys; required by drift checks above.
        assert spec in {spec}
