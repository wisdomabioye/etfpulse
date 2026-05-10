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
    monkeypatch.setattr(settings, "run_bot", False)


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
