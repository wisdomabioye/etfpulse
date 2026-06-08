"""Backtest route tests (PR P2.4 / task #203).

Pinning behaviors:
  - Auth gate (`require_admin_key`) — 503 when key empty, 401 on
    mismatch, 200 on valid key.
  - Window-size cap (`settings.backtest_max_window_days`).
  - `allow_ai=True` requires the `X-Backtest-Allow-AI: yes` header.
  - Unknown detector name → 422 (translated from `ValueError`).
  - Unknown detector kwarg → 422 (translated from `TypeError`).
  - Session `rollback()` runs even when the orchestrator raises —
    verified via a recording session wrapper.
  - GET /detectors returns introspected param signatures for all 5
    detectors so the FE form can render the right input widgets.

Orchestrator correctness is pinned elsewhere (`test_pipeline/test_backtest.py`);
this file mocks `run_backtest` to a canned report so the tests stay
hermetic and fast.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from httpx import ASGITransport

from etfpulse.api.deps import get_db_session
from etfpulse.api.routes import backtest as backtest_route
from etfpulse.app import create_app
from etfpulse.config import settings
from etfpulse.pipeline.backtest import BacktestPerDetector, BacktestReport

_ADMIN_KEY = "secret-key"


def _canned_report() -> BacktestReport:
    """A minimal valid `BacktestReport` for the route to serialise.

    Empty per_detector + outcomes is the smallest fixture that satisfies
    the response schema. Window/dates match the test's request shape so
    a future readability check (`assert body["start"] == ...`) reads
    naturally.
    """
    return BacktestReport(
        start="2026-04-01",
        end="2026-04-07",
        ai_prompt_version="v3",
        detector_configs={"flow_anomaly": {"lookback_days": 14}},
        counters={"hits_total": 0, "scored_total": 0},
        per_detector=[
            BacktestPerDetector(
                detector_name="flow_anomaly",
                n_hits=0,
                n_scored=0,
                wins=0,
                losses=0,
                hit_rate=None,
            ),
        ],
        outcomes=[],
    )


@pytest.fixture
async def client(db_session, monkeypatch) -> AsyncIterator[httpx.AsyncClient]:
    """Test client with the admin key configured + db_session bound.

    `admin_api_key` defaults to empty (admin disabled). Most tests need
    it set; we set it here and individual auth-edge tests override back
    to "" via their own monkeypatch.
    """
    monkeypatch.setattr(settings, "admin_api_key", _ADMIN_KEY)
    app = create_app()

    async def _override() -> AsyncIterator[Any]:
        yield db_session

    app.dependency_overrides[get_db_session] = _override
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def stub_run_backtest(monkeypatch):
    """Replace `run_backtest` with a canned-report coroutine. Records
    every call so tests can assert on the arguments the route forwarded."""
    calls: list[dict[str, Any]] = []

    async def _stub(session, **kwargs):
        calls.append({"session_present": session is not None, **kwargs})
        return _canned_report()

    monkeypatch.setattr(backtest_route, "run_backtest", _stub)
    return calls


@pytest.fixture
def stub_make_resolver(monkeypatch):
    """Neutral resolver stub — the route only forwards it to
    `run_backtest`, which we already mock. Catches a future regression
    where the route ever wires a live caller without an opt-in."""
    captured: list[dict[str, Any]] = []

    def _stub(session, *, allow_live_ai: bool = False, live_ai_caller=None):
        captured.append(
            {"allow_live_ai": allow_live_ai, "live_caller_set": live_ai_caller is not None}
        )

        async def _resolve(_hit):
            return None

        return _resolve

    monkeypatch.setattr(backtest_route, "make_resolver", _stub)
    return captured


class TestAuthGate:
    async def test_disabled_when_admin_key_empty(self, db_session, monkeypatch):
        monkeypatch.setattr(settings, "admin_api_key", "")
        app = create_app()

        async def _override() -> AsyncIterator[Any]:
            yield db_session

        app.dependency_overrides[get_db_session] = _override
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post(
                "/api/admin/backtest",
                json={"start": "2026-04-01", "end": "2026-04-07"},
            )
        assert r.status_code == 503

    async def test_rejects_wrong_admin_key(self, client):
        r = await client.post(
            "/api/admin/backtest",
            headers={"X-Admin-Key": "wrong"},
            json={"start": "2026-04-01", "end": "2026-04-07"},
        )
        assert r.status_code == 401


class TestPostHappyPath:
    async def test_returns_report_shape(self, client, stub_run_backtest, stub_make_resolver):
        r = await client.post(
            "/api/admin/backtest",
            headers={"X-Admin-Key": _ADMIN_KEY},
            json={"start": "2026-04-01", "end": "2026-04-07"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ai_prompt_version"] == "v3"
        assert body["start"] == "2026-04-01"
        assert body["end"] == "2026-04-07"
        assert len(body["per_detector"]) == 1
        assert body["per_detector"][0]["detector_name"] == "flow_anomaly"
        # Resolver was constructed once with the default (off) AI flag.
        assert stub_make_resolver == [{"allow_live_ai": False, "live_caller_set": False}]
        assert len(stub_run_backtest) == 1

    async def test_forwards_detector_overrides(self, client, stub_run_backtest, stub_make_resolver):
        r = await client.post(
            "/api/admin/backtest",
            headers={"X-Admin-Key": _ADMIN_KEY},
            json={
                "start": "2026-04-01",
                "end": "2026-04-07",
                "detector_overrides": {"magnitude": {"percentile_threshold": 0.85}},
            },
        )
        assert r.status_code == 200
        assert stub_run_backtest[0]["detector_overrides"] == {
            "magnitude": {"percentile_threshold": 0.85}
        }


class TestWindowValidation:
    async def test_rejects_end_before_start(self, client, stub_run_backtest):
        r = await client.post(
            "/api/admin/backtest",
            headers={"X-Admin-Key": _ADMIN_KEY},
            json={"start": "2026-04-07", "end": "2026-04-01"},
        )
        assert r.status_code == 422
        assert "end date" in r.json()["detail"]
        assert stub_run_backtest == []  # orchestrator never called

    async def test_rejects_window_over_cap(
        self,
        client,
        stub_run_backtest,
        monkeypatch,
    ):
        # Use a tiny cap so a small window trips it — keeps fixtures
        # readable and doesn't rely on the production default.
        monkeypatch.setattr(settings, "backtest_max_window_days", 3)
        r = await client.post(
            "/api/admin/backtest",
            headers={"X-Admin-Key": _ADMIN_KEY},
            json={"start": "2026-04-01", "end": "2026-04-07"},
        )
        assert r.status_code == 422
        assert "exceeds cap" in r.json()["detail"]
        assert stub_run_backtest == []

    async def test_accepts_window_at_cap_boundary(
        self,
        client,
        stub_run_backtest,
        stub_make_resolver,
        monkeypatch,
    ):
        # Exact-cap window must pass — the cap is INCLUSIVE.
        monkeypatch.setattr(settings, "backtest_max_window_days", 7)
        r = await client.post(
            "/api/admin/backtest",
            headers={"X-Admin-Key": _ADMIN_KEY},
            json={"start": "2026-04-01", "end": "2026-04-07"},
        )
        assert r.status_code == 200


class TestAllowAIGate:
    async def test_rejects_allow_ai_without_header(
        self,
        client,
        stub_run_backtest,
        stub_make_resolver,
    ):
        r = await client.post(
            "/api/admin/backtest",
            headers={"X-Admin-Key": _ADMIN_KEY},
            json={"start": "2026-04-01", "end": "2026-04-07", "allow_ai": True},
        )
        assert r.status_code == 422
        assert "X-Backtest-Allow-AI" in r.json()["detail"]
        assert stub_run_backtest == []

    async def test_accepts_allow_ai_with_header(
        self,
        client,
        stub_run_backtest,
        stub_make_resolver,
    ):
        r = await client.post(
            "/api/admin/backtest",
            headers={
                "X-Admin-Key": _ADMIN_KEY,
                "X-Backtest-Allow-AI": "yes",
            },
            json={"start": "2026-04-01", "end": "2026-04-07", "allow_ai": True},
        )
        assert r.status_code == 200
        # Resolver was constructed with allow_live_ai=True; live_caller
        # stays None until the live-AI wiring lands in a later task.
        assert stub_make_resolver[0]["allow_live_ai"] is True
        assert stub_make_resolver[0]["live_caller_set"] is False

    async def test_header_ignored_when_allow_ai_false(
        self,
        client,
        stub_run_backtest,
        stub_make_resolver,
    ):
        # Header without body flag = no-op (permissive). The gate is
        # one-way: body opt-in requires header confirmation, but a
        # set header on a body-off request must not change behavior.
        r = await client.post(
            "/api/admin/backtest",
            headers={
                "X-Admin-Key": _ADMIN_KEY,
                "X-Backtest-Allow-AI": "yes",
            },
            json={"start": "2026-04-01", "end": "2026-04-07"},
        )
        assert r.status_code == 200
        assert stub_make_resolver[0]["allow_live_ai"] is False


class TestErrorTranslation:
    async def test_unknown_detector_translates_to_422(
        self,
        client,
        monkeypatch,
        stub_make_resolver,
    ):
        async def _raises_value_error(session, **kwargs):
            raise ValueError("unknown detector: foo")

        monkeypatch.setattr(backtest_route, "run_backtest", _raises_value_error)
        r = await client.post(
            "/api/admin/backtest",
            headers={"X-Admin-Key": _ADMIN_KEY},
            json={
                "start": "2026-04-01",
                "end": "2026-04-07",
                "detector_overrides": {"foo": {}},
            },
        )
        assert r.status_code == 422
        assert "unknown detector" in r.json()["detail"]

    async def test_unknown_kwarg_translates_to_422(
        self,
        client,
        monkeypatch,
        stub_make_resolver,
    ):
        async def _raises_type_error(session, **kwargs):
            raise TypeError("__init__() got an unexpected keyword argument 'bogus'")

        monkeypatch.setattr(backtest_route, "run_backtest", _raises_type_error)
        r = await client.post(
            "/api/admin/backtest",
            headers={"X-Admin-Key": _ADMIN_KEY},
            json={
                "start": "2026-04-01",
                "end": "2026-04-07",
                "detector_overrides": {"magnitude": {"bogus": 1}},
            },
        )
        assert r.status_code == 422
        assert "invalid detector override kwargs" in r.json()["detail"]


class TestRollbackInvariant:
    async def test_rollback_runs_even_on_error(
        self,
        client,
        monkeypatch,
        stub_make_resolver,
        db_session,
    ):
        """Read-only contract — session must rollback even when the
        orchestrator raises. Verified via a counting wrapper on the
        session's rollback method."""
        calls: list[None] = []
        original_rollback = db_session.rollback

        async def _counting_rollback():
            calls.append(None)
            await original_rollback()

        monkeypatch.setattr(db_session, "rollback", _counting_rollback)

        async def _raises_value_error(session, **kwargs):
            raise ValueError("test error")

        monkeypatch.setattr(backtest_route, "run_backtest", _raises_value_error)

        r = await client.post(
            "/api/admin/backtest",
            headers={"X-Admin-Key": _ADMIN_KEY},
            json={"start": "2026-04-01", "end": "2026-04-07"},
        )
        assert r.status_code == 422
        assert len(calls) >= 1, "session.rollback() must run in the finally branch"


class TestDetectorsListing:
    async def test_returns_all_known_detectors(self, client):
        r = await client.get(
            "/api/admin/backtest/detectors",
            headers={"X-Admin-Key": _ADMIN_KEY},
        )
        assert r.status_code == 200
        body = r.json()
        names = {d["name"] for d in body["detectors"]}
        assert names == {
            "flow_anomaly",
            "magnitude",
            "acceleration",
            "divergence",
            "regime_shift",
        }

    async def test_includes_signal_type_and_params(self, client):
        r = await client.get(
            "/api/admin/backtest/detectors",
            headers={"X-Admin-Key": _ADMIN_KEY},
        )
        assert r.status_code == 200
        magnitude = next(d for d in r.json()["detectors"] if d["name"] == "magnitude")
        assert magnitude["signal_type"] == "magnitude"
        # Magnitude takes percentile_threshold + lookback_days + min_history_days.
        param_names = {p["name"] for p in magnitude["params"]}
        assert {"percentile_threshold", "lookback_days", "min_history_days"} <= param_names
        # Every param has a recognisable type_name.
        valid_types = {"int", "float", "Decimal", "bool", "str"}
        for p in magnitude["params"]:
            assert p["type_name"] in valid_types

    async def test_decimal_defaults_render_as_strings(self, client):
        """Acceleration's `min_slope_old_usd` is a Decimal — the listing
        must serialise it as a string so the FE preserves precision and
        no scientific-notation surprises hit the form input."""
        r = await client.get(
            "/api/admin/backtest/detectors",
            headers={"X-Admin-Key": _ADMIN_KEY},
        )
        assert r.status_code == 200
        acceleration = next(d for d in r.json()["detectors"] if d["name"] == "acceleration")
        decimal_param = next(p for p in acceleration["params"] if p["name"] == "min_slope_old_usd")
        assert decimal_param["has_default"] is True
        assert isinstance(decimal_param["default"], str)
        assert decimal_param["type_name"] == "Decimal"

    async def test_detectors_endpoint_requires_admin_key(self, db_session, monkeypatch):
        monkeypatch.setattr(settings, "admin_api_key", _ADMIN_KEY)
        app = create_app()

        async def _override() -> AsyncIterator[Any]:
            yield db_session

        app.dependency_overrides[get_db_session] = _override
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.get(
                "/api/admin/backtest/detectors",
                headers={"X-Admin-Key": "wrong"},
            )
        assert r.status_code == 401
