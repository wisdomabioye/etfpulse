"""Exception-handler redaction tests.

We build an app with a few test-only routes that each raise a domain error.
The assertions verify: (a) the HTTP status is 503, (b) the response body is
opaque (no internal message leaks to clients).
"""

from __future__ import annotations

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from etfpulse.adapters.sosovalue import (
    SoSoValueError,
    SoSoValueMonthlyQuotaError,
    SoSoValueRateLimitError,
)
from etfpulse.app import create_app


@pytest.fixture
def client():
    """App with a dedicated /test/* router that raises each domain error."""
    app = create_app()

    router = APIRouter()

    @router.get("/test/soso-quota")
    async def _quota():
        raise SoSoValueMonthlyQuotaError("secret: burned through 100k calls")

    @router.get("/test/soso-ratelimit")
    async def _rate():
        raise SoSoValueRateLimitError("secret: 21 per minute exceeded")

    @router.get("/test/soso-generic")
    async def _generic():
        raise SoSoValueError("secret: parsing failed at offset 42")

    @router.get("/test/db-error")
    async def _db():
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    app.include_router(router, prefix="/api")

    with TestClient(app) as client:
        yield client


def test_monthly_quota_returns_opaque_503(client):
    r = client.get("/api/test/soso-quota")
    assert r.status_code == 503
    body = r.json()
    assert body == {"detail": "upstream quota exceeded"}
    # Internal message must not leak
    assert "secret" not in r.text
    assert "100k" not in r.text


def test_rate_limit_returns_opaque_503(client):
    r = client.get("/api/test/soso-ratelimit")
    assert r.status_code == 503
    assert r.json() == {"detail": "upstream rate limited"}
    assert "secret" not in r.text
    assert "21 per minute" not in r.text


def test_sosovalue_generic_returns_opaque_503(client):
    r = client.get("/api/test/soso-generic")
    assert r.status_code == 503
    assert r.json() == {"detail": "upstream unavailable"}
    assert "secret" not in r.text
    assert "parsing failed" not in r.text


def test_db_error_returns_opaque_503(client):
    r = client.get("/api/test/db-error")
    assert r.status_code == 503
    assert r.json() == {"detail": "database error"}
    assert "connection refused" not in r.text


def test_subclass_matches_specific_handler_not_base(client):
    """SoSoValueMonthlyQuotaError is a subclass of SoSoValueError — Starlette
    must dispatch to the specific handler, not the generic one."""
    r = client.get("/api/test/soso-quota")
    # If the generic handler matched, body would be 'upstream unavailable'
    assert r.json()["detail"] == "upstream quota exceeded"
