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


class TestAccelerationMinPriorUsdConstraint:
    """PR F.1 — `acceleration_min_prior_usd` MUST be strictly positive.

    A zero floor would let `slope_old=0` data reach the
    `second_derivative / slope_old` division in the detector and crash the
    daily cycle mid-tick with ZeroDivisionError. Pydantic's `gt=0`
    constraint surfaces this misconfiguration at app boot instead of
    silently mid-cycle.

    Pre-F.1 the constraint was `ge=0` and the same crash path existed for
    `prior_sum=0` data — exposed by F.1's documentation of the floor's
    zero-divide protection role.
    """

    def test_zero_min_prior_usd_raises_at_boot(self, monkeypatch):
        # Patch the field via env so pydantic-settings parses it through
        # the same path as a production override.
        monkeypatch.setenv("ACCELERATION_MIN_PRIOR_USD", "0")
        with pytest.raises(ValidationError) as exc_info:
            Settings()
        # The Decimal field's gt=0 violation must mention the field name
        # so an operator reading the boot log can find it instantly.
        assert "acceleration_min_prior_usd" in str(exc_info.value).lower()

    def test_negative_min_prior_usd_raises_at_boot(self, monkeypatch):
        """Defensive — pydantic should reject negatives too. `abs(slope_old)`
        in the detector would never be < a negative floor, so the floor
        would effectively never block anything. Surface as ValidationError."""
        monkeypatch.setenv("ACCELERATION_MIN_PRIOR_USD", "-1000000")
        with pytest.raises(ValidationError):
            Settings()

    def test_positive_min_prior_usd_accepted(self, monkeypatch):
        """Defaults work; explicit positive overrides work."""
        monkeypatch.setenv("ACCELERATION_MIN_PRIOR_USD", "500000")
        s = Settings()
        assert s.acceleration_min_prior_usd == Decimal("500000")
