"""Admin trigger route — auth gate + successful cycle invocation.

We mock `_run_cycle_with_session` to a predictable return value rather than
running a full cycle through SoSoValue/OpenRouter on every test.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from etfpulse.app import create_app
from etfpulse.config import settings

_SAMPLE_SUMMARY = {
    "ingested": {"BTC": 2, "ETH": 1},
    "ingest_errors": [],
    "detectors_run": 1,
    "detector_errors": [],
    "signals_new": 1,
    "signals_duplicate": 0,
    "ai_succeeded": 1,
    "ai_failed": 0,
}


@pytest.fixture
def stub_cycle(monkeypatch):
    """Replace the cycle wrapper with a no-op that returns a fixed summary."""
    calls: list[None] = []

    async def _stub() -> dict:
        calls.append(None)
        return _SAMPLE_SUMMARY

    monkeypatch.setattr("etfpulse.api.routes.admin._run_cycle_with_session", _stub)
    return calls


@pytest.fixture
def stub_cycle_failing(monkeypatch):
    """Replace the cycle wrapper to simulate a rollback (returns None)."""

    async def _stub() -> None:
        return None

    monkeypatch.setattr("etfpulse.api.routes.admin._run_cycle_with_session", _stub)


# ---- Auth gate ------------------------------------------------------------


def test_without_key_returns_503_when_admin_disabled(monkeypatch, stub_cycle):
    """ADMIN_API_KEY unset → 503 (admin surface disabled). require_admin_key
    returns 503 BEFORE checking the header value, so even an absent header
    should see this."""
    monkeypatch.setattr(settings, "admin_api_key", "")

    with TestClient(create_app()) as client:
        r = client.post("/api/admin/signals/trigger")

    assert r.status_code == 503
    assert stub_cycle == [], "cycle must not run when admin is disabled"


def test_wrong_key_returns_401(monkeypatch, stub_cycle):
    """Correct env but mismatching header → 401."""
    monkeypatch.setattr(settings, "admin_api_key", "secret-key")

    with TestClient(create_app()) as client:
        r = client.post(
            "/api/admin/signals/trigger",
            headers={"X-Admin-Key": "wrong-key"},
        )

    assert r.status_code == 401
    assert stub_cycle == [], "cycle must not run when key is wrong"


def test_missing_header_with_key_set_returns_401(monkeypatch, stub_cycle):
    """ADMIN_API_KEY set but header absent → 401."""
    monkeypatch.setattr(settings, "admin_api_key", "secret-key")

    with TestClient(create_app()) as client:
        r = client.post("/api/admin/signals/trigger")

    assert r.status_code == 401
    assert stub_cycle == []


# ---- Happy path -----------------------------------------------------------


def test_correct_key_returns_200_and_summary(monkeypatch, stub_cycle):
    monkeypatch.setattr(settings, "admin_api_key", "secret-key")

    with TestClient(create_app()) as client:
        r = client.post(
            "/api/admin/signals/trigger",
            headers={"X-Admin-Key": "secret-key"},
        )

    assert r.status_code == 200
    body = r.json()
    # Contract with #50 post-deploy checks — these keys must be present.
    expected_keys = {
        "ingested",
        "ingest_errors",
        "detectors_run",
        "detector_errors",
        "signals_new",
        "signals_duplicate",
        "ai_succeeded",
        "ai_failed",
    }
    assert set(body.keys()) == expected_keys
    assert body["signals_new"] == 1
    assert len(stub_cycle) == 1


# ---- Cycle failure path ---------------------------------------------------


def test_cycle_rollback_returns_503(monkeypatch, stub_cycle_failing):
    """`_run_cycle_with_session` returns None on rollback → admin gets 503,
    not a confusing 200 with an empty body."""
    monkeypatch.setattr(settings, "admin_api_key", "secret-key")

    with TestClient(create_app()) as client:
        r = client.post(
            "/api/admin/signals/trigger",
            headers={"X-Admin-Key": "secret-key"},
        )

    assert r.status_code == 503
    assert r.json()["detail"] == "cycle failed — see server logs"
