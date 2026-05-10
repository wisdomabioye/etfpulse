"""Translation layer for bot messages (issue #37).

Design choices:

- **Lookup, not gettext.** Translation strings live in Python dicts keyed by
  language tag. Small enough that JSON+IO machinery would be overkill, and
  keeping them in code makes them greppable + reviewable in PRs. Add a
  language by extending `_TRANSLATIONS["xx"]`; missing keys fall through
  to English so partial translations ship safely.

- **No stored preference.** Language is resolved per-update from
  `update.effective_user.language_code` (Telegram's IETF tag). A user who
  wants explicit control changes their Telegram client locale. If we ever
  need per-user override, add a `pref_language` column + a `/lang`
  command — the resolver below is the seam.

- **Region-tag stripped.** `en-US`, `en-GB`, `es-MX` all collapse to the
  primary subtag. We don't ship regional variants today; if we ever
  ship `es-MX`-specific copy, the resolver becomes the natural place
  to layer the lookup (primary subtag first, full tag overrides).

- **No interpolation framework.** Translated strings are either fixed or
  use plain `str.format(...)` with named placeholders. Keeps the layer
  free of templating dependencies. The `t(...)` helper applies kwargs
  via `.format(**kwargs)` when any are passed.

Scope of this initial cut: the smallest set of operator-visible strings
that proves the layer end-to-end. Future surfaces (signal alerts, track-
record output) extend `_TRANSLATIONS` per key as translations land.
"""

from __future__ import annotations

from typing import Final

from telegram import Update

# Canonical language — the fallback target for missing keys and missing
# language tags. English is the source of truth; every other table is a
# (potentially partial) overlay.
DEFAULT_LANG: Final[str] = "en"

# Translation tables. Outer key is the IETF primary subtag; inner keys are
# the message identifiers used by handlers. To add a language:
#   1. Add a new top-level key (e.g., "ru").
#   2. Copy any subset of the English keys, translating the values.
#   3. Anything you don't translate falls through to English automatically.
_TRANSLATIONS: dict[str, dict[str, str]] = {
    "en": {
        # /start — `{assets}` and `{confidence}` interpolated from the
        # caller's current state (target.obj). For a returning user this
        # reflects their actual settings, not the global defaults.
        "welcome.dm": (
            "👋 <b>Welcome to ETFPulse</b>\n\n"
            "You'll receive crypto ETF flow signals here when our detectors fire. "
            "Your preferences: <code>{assets}</code>, confidence ≥ {confidence}.\n\n"
            "Commands:\n"
            "• <code>/prefs assets BTC,ETH</code> — set which assets to watch\n"
            "• <code>/prefs confidence 7</code> — set minimum confidence (1-10)\n"
            "• <code>/unsubscribe</code> / <code>/subscribe</code> — pause / resume\n"
            "• <code>/help</code> — this list again"
        ),
        "welcome.group": (
            "👋 <b>ETFPulse is now monitoring this group</b>\n\n"
            "Signals will be posted here when our detectors fire. Any member can "
            "configure with:\n"
            "• <code>/prefs assets BTC,ETH</code>\n"
            "• <code>/prefs confidence 7</code>\n"
            "• <code>/help</code> for the full command list"
        ),
        # Callback toasts (cap 200 chars per Telegram answerCallbackQuery)
        "callback.not_admin": "Only group admins can change preferences.",
        "callback.last_asset": ("Keep at least one asset selected. Use ⏸ to pause instead."),
        "callback.start_first": "Start with /start first.",
    },
    "es": {
        "welcome.dm": (
            "👋 <b>Bienvenido a ETFPulse</b>\n\n"
            "Recibirás señales de flujos de ETF cripto aquí cuando nuestros "
            "detectores se activen. Tus preferencias: "
            "<code>{assets}</code>, confianza ≥ {confidence}.\n\n"
            "Comandos:\n"
            "• <code>/prefs assets BTC,ETH</code> — elegir activos a vigilar\n"
            "• <code>/prefs confidence 7</code> — confianza mínima (1-10)\n"
            "• <code>/unsubscribe</code> / <code>/subscribe</code> — pausar / reanudar\n"
            "• <code>/help</code> — ver esta lista"
        ),
        "welcome.group": (
            "👋 <b>ETFPulse ahora monitoriza este grupo</b>\n\n"
            "Se publicarán señales aquí cuando nuestros detectores se activen. "
            "Cualquier miembro puede configurar con:\n"
            "• <code>/prefs assets BTC,ETH</code>\n"
            "• <code>/prefs confidence 7</code>\n"
            "• <code>/help</code> para la lista completa"
        ),
        "callback.not_admin": (
            "Solo los administradores del grupo pueden cambiar las preferencias."
        ),
        "callback.last_asset": ("Mantén al menos un activo seleccionado. Usa ⏸ para pausar."),
        "callback.start_first": "Primero ejecuta /start.",
    },
}


def t(key: str, *, lang: str = DEFAULT_LANG, **kwargs: object) -> str:
    """Resolve a translation key in `lang` with English fallback.

    Lookup order:
        1. `_TRANSLATIONS[lang][key]` — exact match.
        2. `_TRANSLATIONS["en"][key]` — fallback when `lang` lacks the key
           (or `lang` itself is unknown).
        3. KeyError — raised when the key is missing from English too,
           which means a handler is asking for an unregistered string.
           Surfacing this loudly catches typos in PR review rather than
           shipping silent empty messages.

    Any extra kwargs are passed to `str.format(**kwargs)`. Strings with
    no placeholders ignore them. A KeyError from `.format()` on a missing
    placeholder name surfaces as a regular Python error — caller bug,
    visible in logs / tests.
    """
    table = _TRANSLATIONS.get(lang, _TRANSLATIONS[DEFAULT_LANG])
    raw = table[key] if key in table else _TRANSLATIONS[DEFAULT_LANG][key]
    return raw.format(**kwargs) if kwargs else raw


def resolve_lang(update: Update) -> str:
    """Return the IETF primary subtag for the user behind `update`.

    Reads `update.effective_user.language_code`, strips any region suffix
    (`en-US` → `en`), and falls back to DEFAULT_LANG when the field is
    absent. Telegram sends `language_code` for most clients but not all
    bots/integrations — defensive fallback keeps the bot speaking to
    everyone, just in English.
    """
    user = update.effective_user
    if user is None or not user.language_code:
        return DEFAULT_LANG
    return user.language_code.split("-", 1)[0].lower()


__all__ = ["DEFAULT_LANG", "resolve_lang", "t"]
