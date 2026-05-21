"""Liveness + readiness tests.

These use httpx.AsyncClient + ASGITransport (rather than FastAPI's TestClient)
because readiness calls the DB via the route handler. TestClient runs handlers
in its own thread-local event loop, while `etfpulse.db.engine` binds to
whichever loop first touched it — they end up mismatched, producing
"attached to a different loop" failures on the second test. AsyncClient keeps
everything on pytest's session loop where the engine was bound at import.

Readiness failure is simulated by monkey-patching `_ping_db` to return False.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest

from etfpulse.app import create_app


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    app = create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as c:
        yield c


async def test_liveness_always_ok(client: httpx.AsyncClient):
    r = await client.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


async def test_liveness_echoes_request_id_header(client: httpx.AsyncClient):
    incoming = "test-req-id-123"
    r = await client.get("/api/health", headers={"X-Request-ID": incoming})
    assert r.status_code == 200
    assert r.headers.get("X-Request-ID") == incoming


async def test_liveness_generates_request_id_when_missing(client: httpx.AsyncClient):
    r = await client.get("/api/health")
    assert r.status_code == 200
    # UUID4 shape — 36 chars with 4 dashes
    assert len(r.headers.get("X-Request-ID", "")) == 36


async def test_liveness_accepts_head(client: httpx.AsyncClient):
    """Orchestrator health probes sometimes use HEAD to skip the body."""
    r = await client.head("/api/health")
    assert r.status_code == 200


async def test_readiness_ok_when_db_reachable(client: httpx.AsyncClient):
    r = await client.get("/api/health/ready")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["db"] == "ok"
    # Config block always present; in dev (default app_env) it's empty.
    assert body["config"] == {"errors": [], "warnings": []}


async def test_readiness_accepts_head(client: httpx.AsyncClient):
    r = await client.head("/api/health/ready")
    assert r.status_code == 200


async def test_readiness_503_when_db_unreachable(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    from etfpulse.api.routes import health

    async def fake_ping(_engine):
        return False

    monkeypatch.setattr(health, "_ping_db", fake_ping)

    r = await client.get("/api/health/ready")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "degraded"
    assert body["db"] == "error"
    # config block is present even on failure
    assert "config" in body


# ---------------------------------------------------------------------------
# Config preflight via /api/health/ready (Tier 1 / Item 5)
# ---------------------------------------------------------------------------


async def test_readiness_503_when_production_missing_sosovalue_key(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    """Production with empty SOSOVALUE_API_KEY → hard error → 503.
    Pod stays alive (logs preserved); LB takes it out of rotation."""
    from etfpulse.config import settings

    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "sosovalue_api_key", "")
    # Avoid tripping the database_url default-detection at the same time —
    # this test is specifically about the api-key error branch.
    monkeypatch.setattr(settings, "database_url", "postgresql+asyncpg://test/test")

    r = await client.get("/api/health/ready")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "degraded"
    # DB is fine (test DB ping works); config is the failure dimension.
    assert body["db"] == "ok"
    assert any("sosovalue_api_key" in e for e in body["config"]["errors"])


async def test_readiness_503_when_production_database_url_is_dev_default(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    """Catches the most-common Coolify-deploy mistake: forgot to inject
    DATABASE_URL, so the app falls through to its dev default."""
    from etfpulse.api.config_check import _DEV_DATABASE_URL_DEFAULT
    from etfpulse.config import settings

    monkeypatch.setattr(settings, "app_env", "production")
    # Set sosovalue_api_key so we isolate the database_url branch.
    monkeypatch.setattr(settings, "sosovalue_api_key", "test-key")
    # Test conftest swaps settings.database_url to the *_test* URL session-wide;
    # explicitly pin to the production-misconfig default we actually check.
    monkeypatch.setattr(settings, "database_url", _DEV_DATABASE_URL_DEFAULT)

    r = await client.get("/api/health/ready")
    assert r.status_code == 503
    body = r.json()
    assert any("database_url" in e and "local-dev default" in e for e in body["config"]["errors"])


async def test_readiness_200_with_warnings_when_production_admin_key_empty(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    """Empty admin key in prod is a warning, not a hard error — admin
    surface is intentionally disabled by design, but operating without
    it is risky. Returns 200 so LB keeps the pod, body carries the
    warning so dashboards can alert."""
    from etfpulse.config import settings

    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "sosovalue_api_key", "test-key")
    monkeypatch.setattr(settings, "openrouter_api_key", "test-or-key")
    monkeypatch.setattr(settings, "database_url", "postgresql+asyncpg://prod/db")
    monkeypatch.setattr(settings, "admin_api_key", "")
    monkeypatch.setattr(settings, "frontend_url", "https://etfpulse.example.com")
    monkeypatch.setattr(settings, "jwt_secret", "test-jwt-secret-32-chars-long-xx")
    # Bot off → no telegram-partial warning either
    monkeypatch.setattr(settings, "run_bot", False)

    r = await client.get("/api/health/ready")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["config"]["errors"] == []
    assert any("admin_api_key" in w for w in body["config"]["warnings"])


async def test_readiness_warns_when_telegram_config_partial(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    """Partial Telegram config (e.g. token set, secret missing) is the
    most common silent-failure pit: `is_bot_enabled` returns False but
    operator thinks the bot is on. Surface as a warning."""
    from etfpulse.config import settings

    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "sosovalue_api_key", "test-key")
    monkeypatch.setattr(settings, "openrouter_api_key", "test-or-key")
    monkeypatch.setattr(settings, "admin_api_key", "test-admin")
    monkeypatch.setattr(settings, "database_url", "postgresql+asyncpg://prod/db")
    monkeypatch.setattr(settings, "frontend_url", "https://etfpulse.example.com")
    monkeypatch.setattr(settings, "jwt_secret", "test-jwt-secret-32-chars-long-xx")
    # Bot ON but only 2 of 4 fields populated.
    monkeypatch.setattr(settings, "run_bot", True)
    monkeypatch.setattr(settings, "telegram_bot_token", "1:abc")
    monkeypatch.setattr(settings, "telegram_public_url", "https://x.example.com")
    monkeypatch.setattr(settings, "telegram_webhook_secret", "")
    monkeypatch.setattr(settings, "telegram_webhook_url_suffix", "")

    r = await client.get("/api/health/ready")
    assert r.status_code == 200
    body = r.json()
    assert any("telegram config is partial" in w for w in body["config"]["warnings"])


async def test_readiness_no_telegram_warning_when_all_four_empty(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    """Bot deliberately off (all four empty + run_bot True OR run_bot
    False) → not a warning. Only PARTIAL config is suspicious."""
    from etfpulse.config import settings

    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "sosovalue_api_key", "test-key")
    monkeypatch.setattr(settings, "openrouter_api_key", "test-or-key")
    monkeypatch.setattr(settings, "admin_api_key", "test-admin")
    monkeypatch.setattr(settings, "database_url", "postgresql+asyncpg://prod/db")
    monkeypatch.setattr(settings, "frontend_url", "https://etfpulse.example.com")
    monkeypatch.setattr(settings, "jwt_secret", "test-jwt-secret-32-chars-long-xx")
    monkeypatch.setattr(settings, "run_bot", True)
    for f in (
        "telegram_bot_token",
        "telegram_public_url",
        "telegram_webhook_secret",
        "telegram_webhook_url_suffix",
    ):
        monkeypatch.setattr(settings, f, "")

    r = await client.get("/api/health/ready")
    assert r.status_code == 200
    body = r.json()
    assert not any("telegram config is partial" in w for w in body["config"]["warnings"])


async def test_readiness_silent_in_development(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    """Default dev environment: empty keys are normal (offline work) and
    the preflight returns clean. Tests stay quiet."""
    from etfpulse.config import settings

    # All defaults: app_env="development", every key empty.
    monkeypatch.setattr(settings, "app_env", "development")
    monkeypatch.setattr(settings, "sosovalue_api_key", "")
    monkeypatch.setattr(settings, "openrouter_api_key", "")
    monkeypatch.setattr(settings, "admin_api_key", "")

    r = await client.get("/api/health/ready")
    assert r.status_code == 200
    body = r.json()
    assert body["config"] == {"errors": [], "warnings": []}
