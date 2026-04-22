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
    assert r.json() == {"status": "ok", "db": "ok"}


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
    assert r.json() == {"status": "degraded", "db": "error"}
