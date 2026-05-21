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
from decimal import Decimal

from etfpulse.config import settings

_DEV_DATABASE_URL_DEFAULT = "postgresql+asyncpg://postgres:postgres@localhost:5432/etfpulse"
_TELEGRAM_FIELD_NAMES: tuple[str, ...] = (
    "telegram_bot_token",
    "telegram_public_url",
    "telegram_webhook_secret",
    "telegram_webhook_url_suffix",
)

# PR D.3 — execution-cap dev default. If prod still carries this value
# the operator hasn't reviewed the cap; surface as a warning so a real
# trading session doesn't start with $10k/day "I forgot to set this."
_EXECUTION_DAILY_NOTIONAL_DEV_DEFAULT = Decimal("10000")
# Above this leverage the cap is generous beyond retail-prudent norms;
# warn but don't block (sophisticated operators may want it).
_EXECUTION_LEVERAGE_PRUDENT_CEILING = 10


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

    # PR D.4 — `jwt_secret` empty in prod is a hard error. `api/auth.py`
    # falls back to a process-local ephemeral secret in dev, but in prod
    # that would invalidate every active session on every container
    # restart (Coolify redeploy, OOM, etc) — silent UX devastation. Fail
    # readiness so deploys never start issuing tokens that won't survive
    # the next restart.
    if not settings.jwt_secret:
        errors.append(
            "jwt_secret is empty — JWT auth would fall back to a "
            "process-local ephemeral secret that invalidates every "
            "session on restart. Set JWT_SECRET (e.g., openssl rand -hex 32)."
        )

    # `frontend_url` empty in prod was a warning pre-H.2. Post-H.2 the
    # Telegram alert was trimmed to skim-only and depends on the "View on
    # web" inline keyboard for reasoning / regime / news / risks. Without
    # `frontend_url` the keyboard is omitted entirely (`build_signal_keyboard`
    # returns None) and recipients lose all access to the full analysis —
    # the alert becomes 6 lines of "trust us, click... nothing." Promoting
    # to a hard error so deploys fail readiness until ops sets it.
    # `localhost` in prod stays a warning below (the button works for the
    # operator but breaks for every other recipient — distinct failure mode).
    if not settings.frontend_url:
        errors.append(
            "frontend_url is empty — Telegram alerts depend on the 'View on "
            "web' inline keyboard for full analysis access (PR H.2). Set "
            "FRONTEND_URL to the public SPA origin."
        )

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

    # PR D.4 — RFC 7518 §3.2 recommends HS256 keys be at least the size
    # of the hash output (32 bytes / 256 bits). pyjwt warns at runtime
    # but the warning lands in mint/verify logs only; the preflight
    # surfaces it once at boot so the operator sees it before tokens
    # start being issued. Hard error level is too strict (operators
    # rotating with `openssl rand -hex 16` produce a 32-char string =
    # 16 bytes of entropy; still weak but functional). Warning is the
    # right level: visible, fixable, doesn't block deploy.
    if settings.jwt_secret and len(settings.jwt_secret) < 32:
        warnings.append(
            f"jwt_secret is short ({len(settings.jwt_secret)} chars) — "
            "RFC 7518 §3.2 recommends ≥32 bytes for HS256. Rotate to "
            "`openssl rand -hex 32` (64 hex chars)."
        )

    # `frontend_url` pointing at localhost in prod: button works for the
    # operator running it locally but breaks for every other recipient.
    # Distinct from "empty" (which is now a hard error above) — here a
    # button DOES render, it just resolves to the recipient's own machine.
    # Warning, not error: a misconfigured deploy still ships partly-useful
    # alerts to anyone on the same local network as the operator.
    if settings.frontend_url and (
        "localhost" in settings.frontend_url or "127.0.0.1" in settings.frontend_url
    ):
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

    # PR D.3 — execution-cap review surface. These don't gate the deploy
    # (operators may legitimately ship with these values) but a "you
    # haven't reviewed this" warning forces a conscious sign-off before
    # live trading.
    if settings.execution_daily_notional_usd_cap == _EXECUTION_DAILY_NOTIONAL_DEV_DEFAULT:
        warnings.append(
            f"execution_daily_notional_usd_cap is the dev default "
            f"({_EXECUTION_DAILY_NOTIONAL_DEV_DEFAULT}) — review before live "
            "trading. Set EXECUTION_DAILY_NOTIONAL_USD_CAP explicitly to "
            "acknowledge the per-user 24h ceiling."
        )
    if settings.execution_max_leverage > _EXECUTION_LEVERAGE_PRUDENT_CEILING:
        warnings.append(
            f"execution_max_leverage={settings.execution_max_leverage} exceeds "
            f"the retail-prudent ceiling ({_EXECUTION_LEVERAGE_PRUDENT_CEILING}). "
            "Confirm this is intentional."
        )

    return ConfigHealth(errors=errors, warnings=warnings)
