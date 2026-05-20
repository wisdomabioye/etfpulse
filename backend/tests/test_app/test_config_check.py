"""Unit tests for `etfpulse.api.config_check`.

These exercise the pure function directly (no HTTP). Route-level
integration is in test_health.py.
"""

from __future__ import annotations

import pytest

from etfpulse.api.config_check import (
    _DEV_DATABASE_URL_DEFAULT,
    _TELEGRAM_FIELD_NAMES,
    check_config_health,
)
from etfpulse.config import settings


@pytest.fixture
def production(monkeypatch):
    """Flip the env to production with sane non-empty defaults so each test
    can monkeypatch back the specific field it cares about."""
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "database_url", "postgresql+asyncpg://prod/db")
    monkeypatch.setattr(settings, "sosovalue_api_key", "soso-key")
    monkeypatch.setattr(settings, "openrouter_api_key", "or-key")
    monkeypatch.setattr(settings, "admin_api_key", "admin-key")
    monkeypatch.setattr(settings, "frontend_url", "https://etfpulse.example.com")
    monkeypatch.setattr(settings, "run_bot", False)
    # `check_config_health` reads `os.environ` directly for the deprecated
    # `ACCELERATION_MIN_PRIOR_USD` warning (#38). A developer .env or CI
    # shell that happens to set it would spuriously add a warning to the
    # "clean prod" baseline. Drop it for every production test; the
    # deprecation-specific test re-sets it explicitly.
    monkeypatch.delenv("ACCELERATION_MIN_PRIOR_USD", raising=False)
    # PR D.3 — production "clean" baseline must override the dev-default
    # execution cap so the new "you haven't reviewed this" warning doesn't
    # spuriously trip the clean-prod test. The leverage default (5) is
    # already under the prudent ceiling so doesn't need overriding.
    from decimal import Decimal  # local import — fixture scope

    monkeypatch.setattr(settings, "execution_daily_notional_usd_cap", Decimal("50000"))


def test_dev_returns_empty_report(monkeypatch):
    """app_env != 'production' → preflight is no-op even with everything empty."""
    monkeypatch.setattr(settings, "app_env", "development")
    monkeypatch.setattr(settings, "sosovalue_api_key", "")

    report = check_config_health()
    assert report.errors == []
    assert report.warnings == []
    assert report.ok is True


def test_production_clean_when_all_required_set(production):
    report = check_config_health()
    assert report.errors == []
    assert report.warnings == []
    assert report.ok is True


def test_production_database_url_at_dev_default_is_error(production, monkeypatch):
    monkeypatch.setattr(settings, "database_url", _DEV_DATABASE_URL_DEFAULT)
    report = check_config_health()
    assert any("database_url" in e and "local-dev default" in e for e in report.errors)
    assert report.ok is False


def test_production_empty_sosovalue_key_is_error(production, monkeypatch):
    monkeypatch.setattr(settings, "sosovalue_api_key", "")
    report = check_config_health()
    assert any("sosovalue_api_key" in e for e in report.errors)
    assert report.ok is False


def test_production_empty_openrouter_key_is_warning(production, monkeypatch):
    monkeypatch.setattr(settings, "openrouter_api_key", "")
    report = check_config_health()
    # Not in errors — D12 makes AI failure non-fatal.
    assert not any("openrouter_api_key" in e for e in report.errors)
    assert any("openrouter_api_key" in w for w in report.warnings)
    assert report.ok is True


def test_production_empty_admin_key_is_warning(production, monkeypatch):
    monkeypatch.setattr(settings, "admin_api_key", "")
    report = check_config_health()
    assert any("admin_api_key" in w for w in report.warnings)
    assert report.ok is True


def test_partial_telegram_config_is_warning(production, monkeypatch):
    """Only some of the 4 telegram fields set + run_bot=True → warning.
    is_bot_enabled would return False silently otherwise."""
    monkeypatch.setattr(settings, "run_bot", True)
    # Set just the bot token; leave the other 3 empty.
    monkeypatch.setattr(settings, "telegram_bot_token", "1:abc")
    monkeypatch.setattr(settings, "telegram_public_url", "")
    monkeypatch.setattr(settings, "telegram_webhook_secret", "")
    monkeypatch.setattr(settings, "telegram_webhook_url_suffix", "")

    report = check_config_health()
    assert any("telegram config is partial" in w for w in report.warnings)


def test_all_telegram_empty_with_run_bot_on_is_not_a_warning(production, monkeypatch):
    """Bot deliberately off (all 4 empty) → no warning. The is_bot_enabled
    check already reads as False and skips bot startup."""
    monkeypatch.setattr(settings, "run_bot", True)
    for f in _TELEGRAM_FIELD_NAMES:
        monkeypatch.setattr(settings, f, "")

    report = check_config_health()
    assert not any("telegram" in w for w in report.warnings)


def test_all_telegram_set_is_not_a_warning(production, monkeypatch):
    monkeypatch.setattr(settings, "run_bot", True)
    monkeypatch.setattr(settings, "telegram_bot_token", "1:abc")
    monkeypatch.setattr(settings, "telegram_public_url", "https://x.example.com")
    monkeypatch.setattr(settings, "telegram_webhook_secret", "s3cr3t")
    monkeypatch.setattr(settings, "telegram_webhook_url_suffix", "abc123")

    report = check_config_health()
    assert not any("telegram" in w for w in report.warnings)


def test_telegram_partial_skipped_when_run_bot_false(production, monkeypatch):
    """run_bot=False → don't even check telegram fields. The whole bot
    surface is administratively disabled; no operator confusion possible."""
    monkeypatch.setattr(settings, "run_bot", False)
    monkeypatch.setattr(settings, "telegram_bot_token", "1:abc")
    monkeypatch.setattr(settings, "telegram_public_url", "")
    monkeypatch.setattr(settings, "telegram_webhook_secret", "")
    monkeypatch.setattr(settings, "telegram_webhook_url_suffix", "")

    report = check_config_health()
    assert not any("telegram" in w for w in report.warnings)


def test_production_empty_frontend_url_is_error(production, monkeypatch):
    """PR H.3 — promoted from warning to hard error. The Telegram alert
    was trimmed to skim-only in PR H.2 and depends on the 'View on web'
    inline keyboard for reasoning / regime / news / risks. With no
    `frontend_url`, `build_signal_keyboard` returns None and the alert
    becomes 6 lines with no path to the full analysis."""
    monkeypatch.setattr(settings, "frontend_url", "")
    report = check_config_health()
    assert any("frontend_url is empty" in e for e in report.errors)
    assert report.ok is False


def test_production_localhost_frontend_url_is_warning(production, monkeypatch):
    """Worse failure mode: `localhost` in prod sends users links to
    their own machine. Surface loudly."""
    monkeypatch.setattr(settings, "frontend_url", "http://localhost:5173")
    report = check_config_health()
    assert any("localhost" in w and "frontend_url" in w for w in report.warnings)


def test_production_deprecated_acceleration_min_prior_usd_is_warning(production, monkeypatch):
    """PR #38 — `ACCELERATION_MIN_PRIOR_USD` was the legacy env name
    (pre-F.1 it floored prior-window SUM; F.1 repurposed it to floor
    |slope_old|). AliasChoices keeps the old name working as a
    deprecation alias, but operators should migrate to
    `ACCELERATION_MIN_SLOPE_OLD_USD`. Production preflight surfaces a
    warning so the migration nudge is visible in deploy logs."""
    monkeypatch.setenv("ACCELERATION_MIN_PRIOR_USD", "750000")
    report = check_config_health()
    assert any("ACCELERATION_MIN_PRIOR_USD is deprecated" in w for w in report.warnings)
    # Hard errors should remain empty — this is a soft-deprecation, not a
    # hard failure (existing deploys must keep booting).
    assert report.errors == []


def test_dev_does_not_emit_acceleration_min_prior_usd_warning(monkeypatch):
    """Deprecation warning is production-gated like every other config
    preflight check. Dev/test runs stay quiet even with the old var set."""
    monkeypatch.setattr(settings, "app_env", "development")
    monkeypatch.setenv("ACCELERATION_MIN_PRIOR_USD", "750000")
    report = check_config_health()
    assert report.warnings == []


# ---------------------------------------------------------------------------
# PR D.3 — execution-cap preflight
# ---------------------------------------------------------------------------


def test_production_execution_daily_cap_at_dev_default_is_warning(production, monkeypatch):
    """Dev default = 10000. Prod must override before live trading; warn
    when it hasn't been touched."""
    from decimal import Decimal

    monkeypatch.setattr(settings, "execution_daily_notional_usd_cap", Decimal("10000"))
    report = check_config_health()
    assert any("execution_daily_notional_usd_cap is the dev default" in w for w in report.warnings)
    # Not a hard error — operator may legitimately ship with 10k, just
    # has to acknowledge it.
    assert report.errors == []


def test_production_execution_leverage_above_prudent_is_warning(production, monkeypatch):
    """Above 10× is permitted but flagged for review."""
    monkeypatch.setattr(settings, "execution_max_leverage", 15)
    report = check_config_health()
    assert any("execution_max_leverage=15" in w for w in report.warnings)
    assert report.errors == []


def test_production_execution_leverage_at_prudent_ceiling_is_clean(production, monkeypatch):
    """Exactly 10 stays clean — only ABOVE the ceiling warns."""
    monkeypatch.setattr(settings, "execution_max_leverage", 10)
    report = check_config_health()
    assert not any("execution_max_leverage" in w for w in report.warnings)


def test_dev_does_not_emit_execution_warnings(monkeypatch):
    """Execution-cap preflight is production-gated. Dev stays quiet
    even with dev-default + high leverage."""
    monkeypatch.setattr(settings, "app_env", "development")
    monkeypatch.setattr(settings, "execution_max_leverage", 20)
    report = check_config_health()
    assert report.errors == []
    assert report.warnings == []
