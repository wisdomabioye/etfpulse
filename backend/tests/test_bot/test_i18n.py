"""Translation layer (issue #37).

Tests pin the behaviour the handlers rely on: known-key lookups in each
language, region-tag stripping, English fallback for missing keys /
unknown languages, and KeyError when a key is missing from English too
(catches handler typos in PR review).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from etfpulse.bot.i18n import _TRANSLATIONS, DEFAULT_LANG, render_command_list, resolve_lang, t


class TestTranslate:
    def test_known_key_in_english(self):
        msg = t("welcome.dm", lang="en")
        assert "Welcome to ETFPulse" in msg

    def test_known_key_in_spanish(self):
        msg = t("welcome.dm", lang="es")
        assert "Bienvenido a ETFPulse" in msg
        # Confirms it's NOT the English string.
        assert "Welcome to ETFPulse" not in msg

    def test_unknown_lang_falls_back_to_english(self):
        # `xx` is not a language we ship.
        msg = t("welcome.dm", lang="xx")
        assert "Welcome to ETFPulse" in msg

    def test_missing_key_in_non_english_falls_back(self):
        """Partial translations ship safely — a key not in `es` falls
        through to English rather than KeyErroring."""
        # Pin the property: there must exist a key NOT in es but in en.
        # If you remove one of these keys from en, choose another or
        # add a deliberately untranslated test fixture key.
        unmapped_keys = set(_TRANSLATIONS["en"]) - set(_TRANSLATIONS["es"])
        assert unmapped_keys or _TRANSLATIONS["es"].keys() == _TRANSLATIONS["en"].keys(), (
            "either es covers everything (no fallback to test) or "
            "this assertion needs the unmapped-key code path"
        )
        # Smoke-test the fallback by patching: add a key only in en,
        # confirm es resolution falls through.
        original = _TRANSLATIONS["en"].copy()
        try:
            _TRANSLATIONS["en"]["test.only_in_english"] = "anchor"
            assert t("test.only_in_english", lang="es") == "anchor"
        finally:
            _TRANSLATIONS["en"].clear()
            _TRANSLATIONS["en"].update(original)

    def test_default_lang_returns_english(self):
        """Calling `t(key)` with no `lang` kwarg uses DEFAULT_LANG. Pinning
        this so a future refactor can't silently make `lang` required."""
        from etfpulse.bot.i18n import t as t_func

        # English assertion — same string as `lang="en"`.
        assert t_func("welcome.dm") == t_func("welcome.dm", lang="en")
        assert "Welcome to ETFPulse" in t_func("welcome.dm")

    def test_translation_tables_have_consistent_key_sets(self):
        """Every non-English language must define a subset of English keys.
        An `es`-only key is orphaned (lookup always finds it via `en` first
        when the lang is en, and via es when lang is es — but the key
        existing only in es means we have no canonical English to fall
        back to). Catches typos and stale translations during PR review."""
        en_keys = set(_TRANSLATIONS["en"])
        for lang, table in _TRANSLATIONS.items():
            if lang == "en":
                continue
            extra = set(table) - en_keys
            assert not extra, (
                f"language {lang!r} has keys not in en: {sorted(extra)}. "
                f"Every translatable string must exist in English first."
            )

    def test_missing_key_in_english_raises(self):
        """Handler typos should fail loudly. A key absent from English
        means no handler-side fallback exists — KeyError is the right
        surface."""
        with pytest.raises(KeyError):
            t("does.not.exist", lang="en")


class TestRenderCommandList:
    """The single function that drives /help, /start welcomes, and the
    Telegram slash-menu. Properties here are load-bearing for every
    user-facing command surface."""

    def test_renders_advertised_commands(self):
        out = render_command_list("en")
        # Each advertised command appears as a `/name` HTML bullet.
        for name in ["start", "prefs", "subscribe", "unsubscribe", "performance", "help"]:
            assert f"<code>/{name}</code>" in out

    def test_excludes_unadvertised_aliases(self):
        """`track_record` is registered (so users who type the underscore
        form still get a response) but explicitly NOT advertised. It must
        never appear in the rendered list."""
        out = render_command_list("en")
        assert "/track_record" not in out

    def test_never_contains_hyphen_form(self):
        """Regression: the old /help block hardcoded `/track-record`.
        Telegram cannot dispatch hyphenated commands; never advertise them."""
        for lang in ("en", "es"):
            assert "/track-record" not in render_command_list(lang)

    def test_spanish_uses_translated_descriptions(self):
        es = render_command_list("es")
        # Spanish description from `cmd.performance.desc.es`.
        assert "historial de rendimiento" in es
        # English description must not leak through when a Spanish
        # translation exists.
        assert "track record" not in es.lower()

    def test_unknown_lang_falls_back_to_english(self):
        """`render_command_list("xx")` should not crash — i18n's English
        fallback applies per-key inside the renderer."""
        assert render_command_list("xx") == render_command_list("en")


class TestResolveLang:
    def _update_with(self, language_code: str | None) -> MagicMock:
        upd = MagicMock()
        if language_code is None:
            # Telegram sometimes omits language_code (bot-to-bot relays,
            # older clients). Field is on the user; mimic that None case.
            upd.effective_user.language_code = None
        else:
            upd.effective_user.language_code = language_code
        return upd

    def test_exact_match(self):
        assert resolve_lang(self._update_with("en")) == "en"
        assert resolve_lang(self._update_with("es")) == "es"

    def test_strips_region_suffix(self):
        assert resolve_lang(self._update_with("en-US")) == "en"
        assert resolve_lang(self._update_with("es-MX")) == "es"
        assert resolve_lang(self._update_with("pt-BR")) == "pt"

    def test_lowercases_subtag(self):
        """IETF tags are technically case-insensitive on the language
        subtag (`EN-us` is valid). Defensive lowercase keeps the lookup
        table keys consistent."""
        assert resolve_lang(self._update_with("EN-US")) == "en"

    def test_missing_language_code_falls_back_to_default(self):
        assert resolve_lang(self._update_with(None)) == DEFAULT_LANG

    def test_empty_string_language_code_falls_back(self):
        """Some clients send empty string instead of omitting the field."""
        assert resolve_lang(self._update_with("")) == DEFAULT_LANG

    def test_missing_effective_user_falls_back(self):
        upd = MagicMock()
        upd.effective_user = None
        assert resolve_lang(upd) == DEFAULT_LANG
