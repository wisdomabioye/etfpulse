"""Liveness + readiness tests.

Readiness failure is simulated by monkey-patching `_ping_db` to return False —
simpler than conjuring a broken engine, and the behaviour under test is "what
does readiness return when the DB is down?", which the patch answers directly.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from etfpulse.app import create_app


@pytest.fixture
def client():
    app = create_app()
    with TestClient(app) as client:
        yield client


def test_liveness_always_ok(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_liveness_echoes_request_id_header(client):
    incoming = "test-req-id-123"
    r = client.get("/api/health", headers={"X-Request-ID": incoming})
    assert r.status_code == 200
    assert r.headers.get("X-Request-ID") == incoming


def test_liveness_generates_request_id_when_missing(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    # UUID4 shape — 36 chars with 4 dashes
    assert len(r.headers.get("X-Request-ID", "")) == 36


def test_readiness_ok_when_db_reachable(client):
    r = client.get("/api/health/ready")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "db": "ok"}


def test_readiness_503_when_db_unreachable(client, monkeypatch):
    from etfpulse.api.routes import health

    async def fake_ping(_engine):
        return False

    monkeypatch.setattr(health, "_ping_db", fake_ping)

    r = client.get("/api/health/ready")
    assert r.status_code == 503
    assert r.json() == {"status": "degraded", "db": "error"}
