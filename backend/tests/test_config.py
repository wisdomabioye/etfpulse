"""Config-layer invariants — URL normalisation is the critical one because
Coolify/Heroku inject `postgres://` URLs that SQLAlchemy 1.4+ rejects outright.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from etfpulse.config import Settings, normalize_database_url


@pytest.mark.parametrize(
    "raw,expected",
    [
        # Heroku/Coolify-style prefix — must be rewritten
        (
            "postgres://user:pw@host:5432/db",
            "postgresql+asyncpg://user:pw@host:5432/db",
        ),
        # Plain postgresql:// with no driver — must pick up +asyncpg
        (
            "postgresql://user:pw@host:5432/db",
            "postgresql+asyncpg://user:pw@host:5432/db",
        ),
        # Already correct — passthrough unchanged
        (
            "postgresql+asyncpg://user:pw@host:5432/db",
            "postgresql+asyncpg://user:pw@host:5432/db",
        ),
        # The real URL Coolify injected in the first failed deploy
        (
            "postgres://postgres:complex_pw@service-id:5432/postgres",
            "postgresql+asyncpg://postgres:complex_pw@service-id:5432/postgres",
        ),
        # Unknown scheme — passthrough (caller's problem)
        ("sqlite:///foo.db", "sqlite:///foo.db"),
    ],
)
def test_normalize_database_url(raw: str, expected: str):
    assert normalize_database_url(raw) == expected


class TestAccelerationMinSlopeOldUsdConstraint:
    """PR F.1 — `acceleration_min_slope_old_usd` MUST be strictly positive.

    A zero floor would let `slope_old=0` data reach the
    `second_derivative / slope_old` division in the detector and crash the
    daily cycle mid-tick with ZeroDivisionError. Pydantic's `gt=0`
    constraint surfaces this misconfiguration at app boot instead of
    silently mid-cycle.

    PR #38 renamed the field from `acceleration_min_prior_usd`; the old
    env-var name is still accepted as a deprecation alias.
    """

    def test_zero_raises_at_boot(self, monkeypatch):
        # Patch the field via env so pydantic-settings parses it through
        # the same path as a production override.
        monkeypatch.setenv("ACCELERATION_MIN_SLOPE_OLD_USD", "0")
        with pytest.raises(ValidationError) as exc_info:
            Settings()
        # Error must mention one of the accepted alias names so an operator
        # reading the boot log can find it instantly.
        msg = str(exc_info.value).lower()
        assert "slope_old" in msg or "prior_usd" in msg

    def test_negative_raises_at_boot(self, monkeypatch):
        """Defensive — pydantic should reject negatives too. `abs(slope_old)`
        in the detector would never be < a negative floor, so the floor
        would effectively never block anything. Surface as ValidationError."""
        monkeypatch.setenv("ACCELERATION_MIN_SLOPE_OLD_USD", "-1000000")
        with pytest.raises(ValidationError):
            Settings()

    def test_positive_accepted(self, monkeypatch):
        """Defaults work; explicit positive overrides work."""
        monkeypatch.setenv("ACCELERATION_MIN_SLOPE_OLD_USD", "500000")
        s = Settings()
        assert s.acceleration_min_slope_old_usd == Decimal("500000")


class TestAccelerationMinPriorUsdDeprecatedAlias:
    """PR #38 — `ACCELERATION_MIN_PRIOR_USD` is the legacy env name (pre-F.1
    it floored prior-window sum; F.1 repurposed it to floor |slope_old|).
    Pydantic AliasChoices keeps the old name readable so existing deploys
    don't break on the rename; the canonical name is
    `ACCELERATION_MIN_SLOPE_OLD_USD`."""

    def test_old_env_name_populates_new_field(self, monkeypatch):
        """The deprecation seam: setting only the old env name must still
        populate `settings.acceleration_min_slope_old_usd`."""
        monkeypatch.setenv("ACCELERATION_MIN_PRIOR_USD", "750000")
        s = Settings()
        assert s.acceleration_min_slope_old_usd == Decimal("750000")

    def test_new_env_name_wins_when_both_set(self, monkeypatch):
        """When an operator has set both during migration, the new
        canonical name takes priority. AliasChoices order is authoritative."""
        monkeypatch.setenv("ACCELERATION_MIN_SLOPE_OLD_USD", "111111")
        monkeypatch.setenv("ACCELERATION_MIN_PRIOR_USD", "999999")
        s = Settings()
        assert s.acceleration_min_slope_old_usd == Decimal("111111")


class TestSodexEnvironmentSettings:
    """PR D.2 — SoDEX environment + per-venue base URL + chainId resolution.

    The wire contract is confirmed via V.3 signed-write capture against
    the live testnet — chainId is single per environment (not per venue),
    base URLs differ only in the trailing `/spot` vs `/perps`. The
    resolved-* properties bake the operator-friendly default while
    leaving room for env overrides (mock servers, alternative gateways).
    """

    def test_default_environment_is_testnet(self):
        """Safe-by-default: a deploy that forgets to set SODEX_ENVIRONMENT
        does NOT accidentally point at mainnet."""
        s = Settings()
        assert s.sodex_environment == "testnet"

    def test_invalid_environment_rejected(self, monkeypatch):
        """The pattern matcher rejects anything that isn't exactly testnet
        or mainnet — e.g. 'TESTNET', 'staging', '' all fail."""
        monkeypatch.setenv("SODEX_ENVIRONMENT", "staging")
        with pytest.raises(ValidationError):
            Settings()

    def test_testnet_chain_id_resolution(self):
        s = Settings()
        assert s.sodex_environment == "testnet"
        assert s.sodex_chain_id == 138565

    def test_mainnet_chain_id_resolution(self, monkeypatch):
        monkeypatch.setenv("SODEX_ENVIRONMENT", "mainnet")
        s = Settings()
        assert s.sodex_chain_id == 286623

    def test_chain_id_env_override(self, monkeypatch):
        """Operator override path — useful for a mock gateway or a future
        chainId bump."""
        monkeypatch.setenv("SODEX_TESTNET_CHAIN_ID", "999999")
        s = Settings()
        assert s.sodex_chain_id == 999999

    def test_default_testnet_base_urls(self):
        """Testnet defaults pin the exact host + path V.2/V.3 captured
        against. If SoDEX changes the gateway host, override via env."""
        s = Settings()
        assert s.sodex_resolved_spot_base_url == "https://testnet-gw.sodex.dev/api/v1/spot"
        assert s.sodex_resolved_perps_base_url == "https://testnet-gw.sodex.dev/api/v1/perps"

    def test_mainnet_base_urls(self, monkeypatch):
        monkeypatch.setenv("SODEX_ENVIRONMENT", "mainnet")
        s = Settings()
        assert s.sodex_resolved_spot_base_url == "https://mainnet-gw.sodex.dev/api/v1/spot"
        assert s.sodex_resolved_perps_base_url == "https://mainnet-gw.sodex.dev/api/v1/perps"

    def test_base_url_override_wins(self, monkeypatch):
        """An explicit override (e.g. for a mock gateway) takes priority
        over the env-derived default. Other URLs are unaffected — each
        venue is independent."""
        monkeypatch.setenv("SODEX_SPOT_BASE_URL", "http://localhost:9000/spot")
        s = Settings()
        assert s.sodex_resolved_spot_base_url == "http://localhost:9000/spot"
        # Perps still derived from environment.
        assert s.sodex_resolved_perps_base_url == "https://testnet-gw.sodex.dev/api/v1/perps"

    def test_http_timeout_default(self):
        s = Settings()
        assert s.sodex_http_timeout_seconds == pytest.approx(10.0)

    def test_http_timeout_must_be_positive(self, monkeypatch):
        monkeypatch.setenv("SODEX_HTTP_TIMEOUT_SECONDS", "0")
        with pytest.raises(ValidationError):
            Settings()

    def test_retry_attempts_floor(self, monkeypatch):
        """At least 1 attempt — 0 would mean "never call" which is nonsense."""
        monkeypatch.setenv("SODEX_HTTP_RETRY_MAX_ATTEMPTS", "0")
        with pytest.raises(ValidationError):
            Settings()
