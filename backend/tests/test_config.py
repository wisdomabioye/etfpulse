"""Config-layer invariants — URL normalisation is the critical one because
Coolify/Heroku inject `postgres://` URLs that SQLAlchemy 1.4+ rejects outright.
"""

from __future__ import annotations

import pytest

from etfpulse.config import normalize_database_url


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
