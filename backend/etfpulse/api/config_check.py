"""Production env-var preflight.

Catches misconfiguration that would otherwise produce silent failures at
runtime — empty `OPENROUTER_API_KEY` (silent AI degradation per D12),
partial Telegram config (`is_bot_enabled` returns False without telling
anyone), missing `ADMIN_API_KEY` in prod (admin surface 503s on every
call), DATABASE_URL stuck at the dev default, etc.

Two consumers:

    1. `/api/health/ready` — runs every probe, surfaces config status
       alongside DB ping. Hard errors return 503; warnings return 200
       with a structured body so ops dashboards can alert on `warnings`
       without taking the pod out of rotation.

    2. Lifespan boot — logs the report once at startup. Errors log at
       ERROR level (deploy logs show red); warnings at WARNING. Useful
       when the deploy probe doesn't fire fast enough to gate the
       rollout.

Production gate: only `app_env == "production"` triggers reporting.
Dev/test environments expect missing keys (offline work) and stay quiet.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from etfpulse.config import settings

_DEV_DATABASE_URL_DEFAULT = "postgresql+asyncpg://postgres:postgres@localhost:5432/etfpulse"
_TELEGRAM_FIELD_NAMES: tuple[str, ...] = (
    "telegram_bot_token",
    "telegram_public_url",
    "telegram_webhook_secret",
    "telegram_webhook_url_suffix",
)


@dataclass(frozen=True)
class ConfigHealth:
    """Structured result of `check_config_health`.

    `errors` and `warnings` are user-readable strings that name the
    setting and the failure mode — never the secret value itself. Empty
    lists mean "all good" (or non-production).
    """

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when there are no hard errors. Warnings are tolerated."""
        return not self.errors


def check_config_health() -> ConfigHealth:
    """Inspect `settings` and return the structured config-health report.

    Pure function — reads `settings`, returns a value. No I/O, no logging.
    Callers (the route + the lifespan task) decide how to surface it.
    """
    errors: list[str] = []
    warnings: list[str] = []

    # In dev/test we expect missing keys (offline work, no live deploy).
    # Returning an empty report keeps local dev quiet AND keeps the
    # readiness probe predictable for tests that don't pin app_env.
    if not settings.is_production:
        return ConfigHealth()

    # --- Hard errors (502 readiness in prod) ----------------------------------

    # `database_url` matching the dev default in production is almost
    # certainly a missed Coolify env injection. We can't validate "points
    # at the right DB" without connecting (DB ping does that separately),
    # but we CAN catch the most common misconfig.
    if settings.database_url == _DEV_DATABASE_URL_DEFAULT:
        errors.append(
            "database_url is set to the local-dev default "
            "(postgres:postgres@localhost) — Coolify env injection likely missing"
        )

    if not settings.sosovalue_api_key:
        errors.append("sosovalue_api_key is empty — every signal cycle will 401")

    # --- Warnings (200 readiness, but ops should fix) -------------------------

    if not settings.openrouter_api_key:
        warnings.append(
            "openrouter_api_key is empty — signals will persist with NULL "
            "ai_analysis (D12: non-fatal, but degrades UX)"
        )

    if not settings.admin_api_key:
        warnings.append(
            "admin_api_key is empty — admin surface (/api/admin/*) is "
            "intentionally disabled but cannot be operated"
        )

    # `frontend_url` empty in prod = signal alerts ship without the
    # "View on web" button (issue #38). Not fatal — the alert text still
    # delivers — but operators almost certainly want the deep link.
    # `localhost` in prod is the worse failure: users get a button that
    # tries to open their own machine. Warn loudly on both.
    if not settings.frontend_url:
        warnings.append(
            "frontend_url is empty — signal alerts will ship without the "
            "'View on web' inline keyboard button (#38). Set FRONTEND_URL "
            "to the public SPA origin."
        )
    elif "localhost" in settings.frontend_url or "127.0.0.1" in settings.frontend_url:
        warnings.append(
            f"frontend_url points at a local address ({settings.frontend_url!r}) — "
            "signal alert buttons will be broken for every recipient. Set "
            "FRONTEND_URL to the public SPA origin."
        )

    # Deprecated env-var alias (#38). `ACCELERATION_MIN_PRIOR_USD` still
    # works via pydantic AliasChoices but the canonical name is
    # `ACCELERATION_MIN_SLOPE_OLD_USD` (the param now floors |slope_old|,
    # not |prior_sum|). Warn so operators migrate before the alias is
    # removed in a future release.
    if "ACCELERATION_MIN_PRIOR_USD" in {k.upper() for k in os.environ}:
        warnings.append(
            "ACCELERATION_MIN_PRIOR_USD is deprecated (#38) — rename to "
            "ACCELERATION_MIN_SLOPE_OLD_USD. The old name is still read as "
            "an alias but will be removed in a future release."
        )

    # Partial Telegram config is a pit: `is_bot_enabled` silently returns
    # False, so the operator thinks the bot is on but it isn't. Surface
    # it loudly. All-four-empty (genuinely off) is fine and not a warning.
    if settings.run_bot:
        present = [f for f in _TELEGRAM_FIELD_NAMES if getattr(settings, f)]
        missing = [f for f in _TELEGRAM_FIELD_NAMES if not getattr(settings, f)]
        if present and missing:
            warnings.append(
                f"telegram config is partial — present: {sorted(present)}, "
                f"missing: {sorted(missing)} (run_bot=True but is_bot_enabled=False)"
            )

    return ConfigHealth(errors=errors, warnings=warnings)
