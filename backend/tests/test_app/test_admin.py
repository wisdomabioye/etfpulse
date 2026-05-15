"""Admin trigger route — auth gate + successful cycle invocation.

We mock `_run_cycle_with_session` to a predictable return value rather than
running a full cycle through SoSoValue/OpenRouter on every test.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport

from etfpulse.api.deps import get_db_session
from etfpulse.app import create_app
from etfpulse.config import settings
from etfpulse.models import (
    ChannelType,
    DeliveryStatus,
    NotificationChannel,
    Signal,
    SignalDelivery,
    SignalStatus,
    User,
)
from etfpulse.pipeline.detectors.base import compute_fingerprint
from etfpulse.pipeline.reapers import DELIVERY_REAPER_ERROR

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


# ---------------------------------------------------------------------------
# GET /api/admin/metrics — operator dashboard (task #15)
# ---------------------------------------------------------------------------


@pytest.fixture
async def metrics_client(db_session, monkeypatch):
    """Async client with db_session override + a fixed admin key."""
    monkeypatch.setattr(settings, "admin_api_key", "secret-key")
    app = create_app()

    async def _override() -> AsyncIterator:
        yield db_session

    app.dependency_overrides[get_db_session] = _override
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def _seed_signal(
    db_session,
    *,
    fp_seed: str,
    status: str = SignalStatus.PENDING.value,
    confidence: int | None = 7,
    expires_at: datetime | None = None,
    asset: str = "BTC",
    signal_type: str = "flow_anomaly",
) -> Signal:
    signal = Signal(
        signal_type=signal_type,
        asset=asset,
        trigger_data={},
        confidence=confidence,
        status=status,
        expires_at=expires_at,
        fingerprint=compute_fingerprint(asset, "metrics-test", fp_seed),
        signal_date=date(2026, 4, 23),
    )
    db_session.add(signal)
    await db_session.flush()
    return signal


async def _seed_delivery(
    db_session,
    signal_id: int,
    *,
    status: str = DeliveryStatus.PENDING.value,
    created_at: datetime | None = None,
    error_message: str | None = None,
) -> SignalDelivery:
    user = User(pref_assets=["BTC"], pref_min_confidence=5)
    db_session.add(user)
    await db_session.flush()
    channel = NotificationChannel(
        user_id=user.id,
        channel_type=ChannelType.TELEGRAM.value,
        channel_identifier=f"chat-{user.id}",
    )
    db_session.add(channel)
    await db_session.flush()
    delivery = SignalDelivery(
        signal_id=signal_id,
        user_id=user.id,
        channel_id=channel.id,
        status=status,
        error_message=error_message,
    )
    db_session.add(delivery)
    await db_session.flush()
    if created_at is not None:
        delivery.created_at = created_at
        await db_session.flush()
    return delivery


class TestAdminMetricsAuth:
    async def test_disabled_when_admin_key_empty(self, db_session, monkeypatch):
        monkeypatch.setattr(settings, "admin_api_key", "")
        app = create_app()

        async def _override() -> AsyncIterator:
            yield db_session

        app.dependency_overrides[get_db_session] = _override
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.get("/api/admin/metrics")
        app.dependency_overrides.clear()
        assert r.status_code == 503

    async def test_wrong_key_returns_401(self, metrics_client):
        r = await metrics_client.get("/api/admin/metrics", headers={"X-Admin-Key": "wrong"})
        assert r.status_code == 401


class TestAdminMetricsShape:
    async def test_empty_db_returns_all_zeros(self, metrics_client):
        r = await metrics_client.get("/api/admin/metrics", headers={"X-Admin-Key": "secret-key"})
        assert r.status_code == 200
        body = r.json()
        assert body["signal_status_counts"] == {"pending": 0, "alerted": 0, "expired": 0}
        assert body["delivery_status_counts"] == {
            "pending": 0,
            "delivered": 0,
            "failed": 0,
            "skipped": 0,
        }
        assert body["signals_overdue_unreaped"] == 0
        assert body["signals_null_confidence"] == 0
        assert body["deliveries_stuck_pending"] == 0
        assert body["deliveries_reaper_failures"] == 0

    async def test_signal_status_grouped_correctly(self, db_session, metrics_client):
        await _seed_signal(db_session, fp_seed="p1", status=SignalStatus.PENDING.value)
        await _seed_signal(db_session, fp_seed="p2", status=SignalStatus.PENDING.value)
        await _seed_signal(db_session, fp_seed="a1", status=SignalStatus.ALERTED.value)
        await _seed_signal(db_session, fp_seed="e1", status=SignalStatus.EXPIRED.value)

        r = await metrics_client.get("/api/admin/metrics", headers={"X-Admin-Key": "secret-key"})
        assert r.status_code == 200
        assert r.json()["signal_status_counts"] == {
            "pending": 2,
            "alerted": 1,
            "expired": 1,
        }

    async def test_overdue_unreaped_counts_only_past_unexpired(self, db_session, metrics_client):
        # Past expires_at + still pending → counted
        await _seed_signal(
            db_session,
            fp_seed="overdue1",
            status=SignalStatus.PENDING.value,
            expires_at=datetime.now(UTC) - timedelta(hours=1),
        )
        # Past expires_at but already EXPIRED → not counted (reaper already ran)
        await _seed_signal(
            db_session,
            fp_seed="already-expired",
            status=SignalStatus.EXPIRED.value,
            expires_at=datetime.now(UTC) - timedelta(hours=1),
        )
        # Future expires_at → not counted
        await _seed_signal(
            db_session,
            fp_seed="future",
            status=SignalStatus.PENDING.value,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        # NULL expires_at → not counted (AI-failed, indeterminate horizon)
        await _seed_signal(
            db_session,
            fp_seed="null-exp",
            status=SignalStatus.PENDING.value,
            confidence=None,
            expires_at=None,
        )

        r = await metrics_client.get("/api/admin/metrics", headers={"X-Admin-Key": "secret-key"})
        assert r.json()["signals_overdue_unreaped"] == 1

    async def test_null_confidence_count(self, db_session, metrics_client):
        await _seed_signal(db_session, fp_seed="nc1", confidence=None)
        await _seed_signal(db_session, fp_seed="nc2", confidence=None)
        await _seed_signal(db_session, fp_seed="ok", confidence=7)

        r = await metrics_client.get("/api/admin/metrics", headers={"X-Admin-Key": "secret-key"})
        assert r.json()["signals_null_confidence"] == 2

    async def test_stuck_pending_uses_threshold(self, db_session, metrics_client):
        sig = await _seed_signal(db_session, fp_seed="d-stuck")
        cutoff = settings.delivery_pending_max_age_seconds
        # Older than threshold AND pending AND never attempted → counted
        await _seed_delivery(
            db_session,
            sig.id,
            status=DeliveryStatus.PENDING.value,
            created_at=datetime.now(UTC) - timedelta(seconds=cutoff + 60),
        )
        # Pending but fresh → not counted
        await _seed_delivery(
            db_session,
            sig.id,
            status=DeliveryStatus.PENDING.value,
            created_at=datetime.now(UTC),
        )
        # Old but already FAILED → not counted (terminal state)
        await _seed_delivery(
            db_session,
            sig.id,
            status=DeliveryStatus.FAILED.value,
            created_at=datetime.now(UTC) - timedelta(seconds=cutoff + 60),
        )

        r = await metrics_client.get("/api/admin/metrics", headers={"X-Admin-Key": "secret-key"})
        assert r.json()["deliveries_stuck_pending"] == 1

    async def test_stuck_pending_excludes_retrying_rows(self, db_session, metrics_client):
        """Branch 2: a PENDING row that has been ATTEMPTED at least once
        (last_attempt_at IS NOT NULL) is in the worker's exponential-backoff
        retry cycle. It must NOT count as stuck — the metric mirrors the
        reaper's WHERE clause."""
        sig = await _seed_signal(db_session, fp_seed="d-retrying")
        cutoff = settings.delivery_pending_max_age_seconds

        # Old, pending, AND has been attempted → actively retrying, NOT stuck.
        d = await _seed_delivery(
            db_session,
            sig.id,
            status=DeliveryStatus.PENDING.value,
            created_at=datetime.now(UTC) - timedelta(seconds=cutoff + 60),
        )
        d.attempt_count = 2
        d.last_attempt_at = datetime.now(UTC) - timedelta(seconds=15)
        await db_session.flush()

        r = await metrics_client.get("/api/admin/metrics", headers={"X-Admin-Key": "secret-key"})
        assert r.json()["deliveries_stuck_pending"] == 0

    async def test_reaper_failures_match_sentinel(self, db_session, metrics_client):
        sig = await _seed_signal(db_session, fp_seed="d-reaper")
        # Reaper sentinel → counted
        await _seed_delivery(
            db_session,
            sig.id,
            status=DeliveryStatus.FAILED.value,
            error_message=DELIVERY_REAPER_ERROR,
        )
        await _seed_delivery(
            db_session,
            sig.id,
            status=DeliveryStatus.FAILED.value,
            error_message=DELIVERY_REAPER_ERROR,
        )
        # Different failure cause → not counted
        await _seed_delivery(
            db_session,
            sig.id,
            status=DeliveryStatus.FAILED.value,
            error_message="bot blocked by user",
        )

        r = await metrics_client.get("/api/admin/metrics", headers={"X-Admin-Key": "secret-key"})
        assert r.json()["deliveries_reaper_failures"] == 2

    async def test_scheduler_jobs_null_when_scheduler_off(self, db_session, metrics_client):
        """Default test mode disables the scheduler (autouse fixture) →
        `app.state.scheduler` never gets set → field is null."""
        r = await metrics_client.get("/api/admin/metrics", headers={"X-Admin-Key": "secret-key"})
        assert r.json()["scheduler_jobs"] is None

    async def test_scheduler_jobs_lists_registered_jobs(self, db_session, monkeypatch):
        """With a scheduler attached, every registered job surfaces with
        id / next_run_at / trigger / pending. Uses a stub object so we
        don't have to boot APScheduler in tests."""
        monkeypatch.setattr(settings, "admin_api_key", "secret-key")

        class _StubJob:
            def __init__(self, jid: str, next_run, trigger: str, pending: bool):
                self.id = jid
                self.next_run_time = next_run
                self.trigger = trigger
                self.pending = pending

        class _StubScheduler:
            def __init__(self, jobs):
                self._jobs = jobs

            def get_jobs(self):
                return self._jobs

        fixed_next = datetime(2026, 5, 11, 4, 30, tzinfo=UTC)
        stub = _StubScheduler(
            [
                _StubJob("daily_cycle", fixed_next, "cron[hour='4', minute='30']", False),
                _StubJob("signal_expiry_reaper", None, "interval[0:15:00]", True),
            ]
        )

        app = create_app()
        app.state.scheduler = stub

        async def _override() -> AsyncIterator:
            yield db_session

        app.dependency_overrides[get_db_session] = _override
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.get("/api/admin/metrics", headers={"X-Admin-Key": "secret-key"})
        app.dependency_overrides.clear()

        assert r.status_code == 200
        jobs = r.json()["scheduler_jobs"]
        assert isinstance(jobs, list)
        assert len(jobs) == 2
        by_id = {j["id"]: j for j in jobs}
        assert by_id["daily_cycle"]["next_run_at"] == "2026-05-11T04:30:00Z"
        assert by_id["daily_cycle"]["pending"] is False
        assert by_id["daily_cycle"]["trigger"] == "cron[hour='4', minute='30']"
        assert by_id["signal_expiry_reaper"]["next_run_at"] is None
        assert by_id["signal_expiry_reaper"]["pending"] is True

    async def test_delivery_status_grouped(self, db_session, metrics_client):
        sig = await _seed_signal(db_session, fp_seed="grouped")
        await _seed_delivery(db_session, sig.id, status=DeliveryStatus.PENDING.value)
        await _seed_delivery(db_session, sig.id, status=DeliveryStatus.DELIVERED.value)
        await _seed_delivery(db_session, sig.id, status=DeliveryStatus.DELIVERED.value)
        await _seed_delivery(db_session, sig.id, status=DeliveryStatus.FAILED.value)
        await _seed_delivery(db_session, sig.id, status=DeliveryStatus.SKIPPED.value)

        r = await metrics_client.get("/api/admin/metrics", headers={"X-Admin-Key": "secret-key"})
        assert r.json()["delivery_status_counts"] == {
            "pending": 1,
            "delivered": 2,
            "failed": 1,
            "skipped": 1,
        }

    async def test_current_ai_prompt_version_is_active_string(self, db_session, metrics_client):
        """Issue #32 — operator sees what version this process stamps on new
        signals. Sourced from `pipeline.analysis.AI_PROMPT_VERSION`."""
        from etfpulse.pipeline.analysis import AI_PROMPT_VERSION

        r = await metrics_client.get("/api/admin/metrics", headers={"X-Admin-Key": "secret-key"})
        assert r.json()["current_ai_prompt_version"] == AI_PROMPT_VERSION

    async def test_prompt_version_counts_group_correctly(self, db_session, metrics_client):
        """Distribution should reflect actual `signals.ai_prompt_version`
        values, sorted by count descending."""
        # 3× v3, 2× v2, 1× v1
        for i, pv in enumerate(["v3", "v3", "v3", "v2", "v2", "v1"]):
            await _seed_signal(db_session, fp_seed=f"pv{i}")
            # _seed_signal stamps v1 (the model server_default); override.
            await db_session.execute(
                Signal.__table__.update()
                .where(Signal.fingerprint == compute_fingerprint("BTC", "metrics-test", f"pv{i}"))
                .values(ai_prompt_version=pv)
            )

        r = await metrics_client.get("/api/admin/metrics", headers={"X-Admin-Key": "secret-key"})
        counts = r.json()["signal_counts_by_prompt_version"]
        assert counts == {"v3": 3, "v2": 2, "v1": 1}
        # Dict order is by count DESC — JSON preserves Python dict order.
        assert list(counts.keys()) == ["v3", "v2", "v1"]

    async def test_prompt_version_counts_tiebreak_is_deterministic(
        self, db_session, metrics_client
    ):
        """When two cohorts have equal counts, sort by version string for
        stable display. Without the tie-break the dashboard would flicker
        between renders on equal-count cohorts."""
        # 2× v3, 2× v2 — equal counts.
        for i, pv in enumerate(["v2", "v2", "v3", "v3"]):
            await _seed_signal(db_session, fp_seed=f"tie{i}")
            await db_session.execute(
                Signal.__table__.update()
                .where(Signal.fingerprint == compute_fingerprint("BTC", "metrics-test", f"tie{i}"))
                .values(ai_prompt_version=pv)
            )

        r = await metrics_client.get("/api/admin/metrics", headers={"X-Admin-Key": "secret-key"})
        counts = r.json()["signal_counts_by_prompt_version"]
        # v2 sorts before v3 lexicographically; with equal counts the
        # secondary ORDER BY pins v2 first.
        assert list(counts.keys()) == ["v2", "v3"]

    async def test_prompt_version_counts_empty_db(self, db_session, metrics_client):
        r = await metrics_client.get("/api/admin/metrics", headers={"X-Admin-Key": "secret-key"})
        assert r.json()["signal_counts_by_prompt_version"] == {}

    async def test_accepted_webhook_secrets_null_when_bot_off(self, db_session, metrics_client):
        """Default test mode disables the bot → `telegram_webhook_secrets`
        never gets set on app.state → field is null. Distinguishes
        "bot off" from "rotation in progress" (which would be 2)."""
        r = await metrics_client.get("/api/admin/metrics", headers={"X-Admin-Key": "secret-key"})
        assert r.json()["accepted_webhook_secrets"] is None

    async def test_accepted_webhook_secrets_reflects_state(self, db_session, monkeypatch):
        """When bot is up, the field reports `len(app.state.telegram_webhook_secrets)`.
        Steady state is 1; 2+ would indicate a stuck/in-flight rotation."""
        monkeypatch.setattr(settings, "admin_api_key", "secret-key")
        app = create_app()
        # Simulate "rotation in progress" — two accepted secrets.
        app.state.telegram_webhook_secrets = {"old-secret-value", "new-secret-value"}

        async def _override() -> AsyncIterator:
            yield db_session

        app.dependency_overrides[get_db_session] = _override
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.get("/api/admin/metrics", headers={"X-Admin-Key": "secret-key"})
        app.dependency_overrides.clear()
        assert r.status_code == 200
        assert r.json()["accepted_webhook_secrets"] == 2


class TestSignalsAlertedWithZeroDeliveries:
    """Branch 5 — `signals_alerted_with_zero_deliveries` surfaces signals
    that fanned out to nobody. The thread that introduced Branch 5 found
    today's signals with confidence 3-4 producing zero recipient matches
    because every user had pref_min_confidence=6. This metric makes that
    invisible-failure case observable."""

    async def test_empty_db_returns_zero(self, metrics_client):
        r = await metrics_client.get("/api/admin/metrics", headers={"X-Admin-Key": "secret-key"})
        assert r.json()["signals_alerted_with_zero_deliveries"] == 0

    async def test_alerted_without_deliveries_counts(self, db_session, metrics_client):
        """An ALERTED signal with zero `signal_deliveries` rows is what
        we want to surface."""
        await _seed_signal(db_session, fp_seed="zero-1", status=SignalStatus.ALERTED.value)
        await _seed_signal(db_session, fp_seed="zero-2", status=SignalStatus.ALERTED.value)

        r = await metrics_client.get("/api/admin/metrics", headers={"X-Admin-Key": "secret-key"})
        assert r.json()["signals_alerted_with_zero_deliveries"] == 2

    async def test_alerted_with_deliveries_excluded(self, db_session, metrics_client):
        """ALERTED signals that DO have deliveries don't count — those
        are the healthy case, not what we're surfacing."""
        sig = await _seed_signal(db_session, fp_seed="ok", status=SignalStatus.ALERTED.value)
        await _seed_delivery(db_session, sig.id, status=DeliveryStatus.DELIVERED.value)

        r = await metrics_client.get("/api/admin/metrics", headers={"X-Admin-Key": "secret-key"})
        assert r.json()["signals_alerted_with_zero_deliveries"] == 0

    async def test_pending_signals_excluded(self, db_session, metrics_client):
        """PENDING signals aren't fanned-out-with-zero-deliveries — they
        just haven't been fanned out yet. The metric is specifically about
        ALERTED (post-fan-out) status."""
        await _seed_signal(db_session, fp_seed="pending", status=SignalStatus.PENDING.value)

        r = await metrics_client.get("/api/admin/metrics", headers={"X-Admin-Key": "secret-key"})
        assert r.json()["signals_alerted_with_zero_deliveries"] == 0

    async def test_expired_signals_excluded(self, db_session, metrics_client):
        """EXPIRED signals are terminal — counting them would conflate
        "never delivered" with "delivered but expired"."""
        await _seed_signal(db_session, fp_seed="exp", status=SignalStatus.EXPIRED.value)

        r = await metrics_client.get("/api/admin/metrics", headers={"X-Admin-Key": "secret-key"})
        assert r.json()["signals_alerted_with_zero_deliveries"] == 0


class TestDeliveryTraceRoute:
    """Branch 5 — `GET /api/admin/signals/{id}/delivery-trace` is the
    "why didn't this signal reach me?" diagnostic. Mirrors fan-out's
    `_match_users` / `_match_groups` rules so the trace is authoritative."""

    async def test_404_when_signal_not_found(self, metrics_client):
        r = await metrics_client.get(
            "/api/admin/signals/999999/delivery-trace",
            headers={"X-Admin-Key": "secret-key"},
        )
        assert r.status_code == 404

    async def test_disabled_when_admin_key_empty(self, db_session, monkeypatch):
        monkeypatch.setattr(settings, "admin_api_key", "")
        sig = await _seed_signal(db_session, fp_seed="auth-trace")
        app = create_app()

        async def _override() -> AsyncIterator:
            yield db_session

        app.dependency_overrides[get_db_session] = _override
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.get(f"/api/admin/signals/{sig.id}/delivery-trace")
        app.dependency_overrides.clear()
        assert r.status_code == 503

    async def test_user_matched_when_all_filters_pass(self, db_session, metrics_client):
        """Happy path — the user matches every filter; trace says so AND
        no exclude_reason."""
        sig = await _seed_signal(
            db_session, fp_seed="match", status=SignalStatus.ALERTED.value, confidence=8
        )
        user = User(pref_assets=["BTC"], pref_min_confidence=5)
        db_session.add(user)
        await db_session.flush()
        channel = NotificationChannel(
            user_id=user.id,
            channel_type=ChannelType.TELEGRAM.value,
            channel_identifier="111",
        )
        db_session.add(channel)
        await db_session.flush()

        r = await metrics_client.get(
            f"/api/admin/signals/{sig.id}/delivery-trace",
            headers={"X-Admin-Key": "secret-key"},
        )
        assert r.status_code == 200
        body = r.json()
        recipients = body["recipients"]
        assert len(recipients) == 1
        rec = recipients[0]
        assert rec["kind"] == "user"
        assert rec["matched"] is True
        assert rec["exclude_reason"] is None
        assert rec["asset_match"] is True
        assert rec["confidence_match"] is True
        assert body["matched_count"] == 1

    async def test_user_excluded_by_confidence_floor(self, db_session, metrics_client):
        """The exact bug we diagnosed via SQL in the thread: signal conf 4,
        user floor 6. Trace must say so unambiguously."""
        sig = await _seed_signal(db_session, fp_seed="low-conf", confidence=4)
        user = User(pref_assets=["BTC"], pref_min_confidence=6)
        db_session.add(user)
        await db_session.flush()
        channel = NotificationChannel(
            user_id=user.id,
            channel_type=ChannelType.TELEGRAM.value,
            channel_identifier="222",
        )
        db_session.add(channel)
        await db_session.flush()

        r = await metrics_client.get(
            f"/api/admin/signals/{sig.id}/delivery-trace",
            headers={"X-Admin-Key": "secret-key"},
        )
        rec = r.json()["recipients"][0]
        assert rec["matched"] is False
        assert rec["confidence_match"] is False
        assert "confidence 4" in rec["exclude_reason"]
        assert "pref_min_confidence (6)" in rec["exclude_reason"]
        assert r.json()["matched_count"] == 0

    async def test_user_excluded_by_inactive_channel(self, db_session, metrics_client):
        """The other "silent failure" path: channel auto-deactivated after
        a prior Blocked / ChatNotFound. Trace must call this out with a
        hint about the cause."""
        sig = await _seed_signal(db_session, fp_seed="inactive-ch", confidence=8)
        user = User(pref_assets=["BTC"], pref_min_confidence=5)
        db_session.add(user)
        await db_session.flush()
        channel = NotificationChannel(
            user_id=user.id,
            channel_type=ChannelType.TELEGRAM.value,
            channel_identifier="333",
            is_active=False,
        )
        db_session.add(channel)
        await db_session.flush()

        r = await metrics_client.get(
            f"/api/admin/signals/{sig.id}/delivery-trace",
            headers={"X-Admin-Key": "secret-key"},
        )
        rec = r.json()["recipients"][0]
        assert rec["matched"] is False
        assert rec["channel_active"] is False
        assert "channel inactive" in rec["exclude_reason"].lower()

    async def test_user_excluded_by_asset_mismatch(self, db_session, metrics_client):
        """Signal asset isn't in the user's pref_assets list."""
        sig = await _seed_signal(db_session, fp_seed="asset")  # BTC signal
        user = User(pref_assets=["ETH"], pref_min_confidence=5)
        db_session.add(user)
        await db_session.flush()
        channel = NotificationChannel(
            user_id=user.id,
            channel_type=ChannelType.TELEGRAM.value,
            channel_identifier="444",
        )
        db_session.add(channel)
        await db_session.flush()

        r = await metrics_client.get(
            f"/api/admin/signals/{sig.id}/delivery-trace",
            headers={"X-Admin-Key": "secret-key"},
        )
        rec = r.json()["recipients"][0]
        assert rec["matched"] is False
        assert rec["asset_match"] is False
        assert "'BTC'" in rec["exclude_reason"]
        assert "'ETH'" in rec["exclude_reason"]

    async def test_null_confidence_excludes_everyone(self, db_session, metrics_client):
        """AI-failed signals (confidence IS NULL) never deliver — the
        confidence-floor comparison fails for everyone. Trace must say
        "signal has no confidence" so operators don't think it's a per-
        user config issue."""
        sig = await _seed_signal(db_session, fp_seed="no-conf", confidence=None)
        user = User(pref_assets=["BTC"], pref_min_confidence=5)
        db_session.add(user)
        await db_session.flush()
        channel = NotificationChannel(
            user_id=user.id,
            channel_type=ChannelType.TELEGRAM.value,
            channel_identifier="555",
        )
        db_session.add(channel)
        await db_session.flush()

        r = await metrics_client.get(
            f"/api/admin/signals/{sig.id}/delivery-trace",
            headers={"X-Admin-Key": "secret-key"},
        )
        rec = r.json()["recipients"][0]
        assert rec["matched"] is False
        assert "no confidence" in rec["exclude_reason"]

    async def test_existing_delivery_row_state_inlined(self, db_session, metrics_client):
        """When a SignalDelivery row exists for the (signal, user) pair,
        its status / attempts / error_message land in the trace inline.
        Operators reading the trace can see "delivery exists, but it
        failed because Y" without joining another query."""
        sig = await _seed_signal(
            db_session, fp_seed="with-delivery", status=SignalStatus.ALERTED.value, confidence=8
        )
        user = User(pref_assets=["BTC"], pref_min_confidence=5)
        db_session.add(user)
        await db_session.flush()
        channel = NotificationChannel(
            user_id=user.id,
            channel_type=ChannelType.TELEGRAM.value,
            channel_identifier="666",
        )
        db_session.add(channel)
        await db_session.flush()
        delivery = SignalDelivery(
            signal_id=sig.id,
            user_id=user.id,
            channel_id=channel.id,
            status=DeliveryStatus.FAILED.value,
            attempt_count=3,
            error_message="rate limited",
        )
        db_session.add(delivery)
        await db_session.flush()

        r = await metrics_client.get(
            f"/api/admin/signals/{sig.id}/delivery-trace",
            headers={"X-Admin-Key": "secret-key"},
        )
        body = r.json()
        rec = body["recipients"][0]
        assert rec["delivery_status"] == "failed"
        assert rec["delivery_attempts"] == 3
        assert rec["delivery_error"] == "rate limited"
        assert body["delivery_count"] == 1
        assert body["failed_count"] == 1

    async def test_group_filters_evaluated_separately(self, db_session, metrics_client):
        """A TelegramGroup has the same filter rules minus the channel
        join. Trace shows kind='group' and channel_active=None (n/a)."""
        from etfpulse.models import TelegramGroup

        sig = await _seed_signal(db_session, fp_seed="grp", confidence=8)
        group = TelegramGroup(
            chat_id=-100777,
            title="Alpha Squad",
            pref_assets=["BTC"],
            pref_min_confidence=5,
        )
        db_session.add(group)
        await db_session.flush()

        r = await metrics_client.get(
            f"/api/admin/signals/{sig.id}/delivery-trace",
            headers={"X-Admin-Key": "secret-key"},
        )
        body = r.json()
        groups = [rec for rec in body["recipients"] if rec["kind"] == "group"]
        assert len(groups) == 1
        g = groups[0]
        assert g["matched"] is True
        assert g["channel_active"] is None  # n/a for groups
        assert g["target_label"] == "Alpha Squad"
        assert g["chat_id"] == -100777

    async def test_counts_aggregate_across_recipients(self, db_session, metrics_client):
        """Multiple users + multiple deliveries — count fields must sum
        correctly across the recipient set."""
        sig = await _seed_signal(
            db_session, fp_seed="multi", status=SignalStatus.ALERTED.value, confidence=8
        )
        for i in range(3):
            user = User(pref_assets=["BTC"], pref_min_confidence=5)
            db_session.add(user)
            await db_session.flush()
            ch = NotificationChannel(
                user_id=user.id,
                channel_type=ChannelType.TELEGRAM.value,
                channel_identifier=f"800{i}",
            )
            db_session.add(ch)
            await db_session.flush()
            # 2 delivered, 1 failed.
            status_val = DeliveryStatus.DELIVERED.value if i < 2 else DeliveryStatus.FAILED.value
            db_session.add(
                SignalDelivery(
                    signal_id=sig.id, user_id=user.id, channel_id=ch.id, status=status_val
                )
            )
            await db_session.flush()

        r = await metrics_client.get(
            f"/api/admin/signals/{sig.id}/delivery-trace",
            headers={"X-Admin-Key": "secret-key"},
        )
        body = r.json()
        assert body["matched_count"] == 3
        assert body["delivery_count"] == 3
        assert body["delivered_count"] == 2
        assert body["failed_count"] == 1
        assert body["pending_count"] == 0


class TestDeliveryTraceConsistency:
    """Pins the trace endpoint's `matched` verdict against `fan_out_signal`'s
    actual SQL-side filter behaviour. This is the safety net against the
    drift hazard called out in `pipeline.delivery._match_users` /
    `_match_groups`: if a future maintainer adds a filter rule to the SQL
    side but forgets to mirror it in `_trace_user` / `_trace_group`, the
    trace would silently report `matched=True` for a recipient that
    fan-out actually skips. This test seeds a mix of users that exercise
    every filter, runs fan-out, runs the trace, and asserts the two
    populations agree on every recipient."""

    async def test_trace_matched_set_equals_fan_out_inserted_set(self, db_session, metrics_client):
        from etfpulse.pipeline.delivery import fan_out_signal

        sig = await _seed_signal(
            db_session,
            fp_seed="consistency",
            status=SignalStatus.PENDING.value,  # fan_out_signal requires PENDING
            confidence=7,
        )

        # Seed one of each "interesting" filter outcome. Labels keep the
        # assertion messages self-documenting; the key invariant is
        # trace.matched ↔ fan-out insert.
        cases = [
            # label, kwargs for User, kwargs for NotificationChannel
            ("active_match", {}, {}),
            ("inactive_user", {"is_active": False}, {}),
            ("paused_user", {"pref_paused": True}, {}),
            ("inactive_channel", {}, {"is_active": False}),
            ("asset_mismatch", {"pref_assets": ["ETH"]}, {}),
            ("confidence_too_high", {"pref_min_confidence": 9}, {}),
        ]
        for idx, (_label, u_kwargs, c_kwargs) in enumerate(cases):
            user = User(
                pref_assets=u_kwargs.get("pref_assets", ["BTC"]),
                pref_min_confidence=u_kwargs.get("pref_min_confidence", 5),
                is_active=u_kwargs.get("is_active", True),
                pref_paused=u_kwargs.get("pref_paused", False),
            )
            db_session.add(user)
            await db_session.flush()
            db_session.add(
                NotificationChannel(
                    user_id=user.id,
                    channel_type=ChannelType.TELEGRAM.value,
                    channel_identifier=f"consist-{idx}",
                    is_active=c_kwargs.get("is_active", True),
                )
            )
        await db_session.flush()

        # Run REAL fan-out. Returns the count of new SignalDelivery rows.
        inserted = await fan_out_signal(db_session, sig.id)
        await db_session.flush()

        r = await metrics_client.get(
            f"/api/admin/signals/{sig.id}/delivery-trace",
            headers={"X-Admin-Key": "secret-key"},
        )
        body = r.json()

        # Invariant 1: the trace's matched_count equals the count of rows
        # fan-out actually inserted. The most-direct anti-drift assertion.
        assert body["matched_count"] == inserted, (
            f"trace.matched_count={body['matched_count']} but fan-out "
            f"inserted={inserted}; one side filters differently"
        )

        # Invariant 2: every matched recipient has a SignalDelivery row
        # (delivery_status not None), and every non-matched recipient does
        # not. Pin the pairing per-row, not just the aggregate count.
        for rec in body["recipients"]:
            if rec["matched"]:
                assert rec["delivery_status"] is not None, (
                    f"matched recipient {rec['target_id']} ({rec['kind']}) "
                    f"has no SignalDelivery row — fan-out didn't insert it"
                )
            else:
                assert rec["delivery_status"] is None, (
                    f"NON-matched recipient {rec['target_id']} ({rec['kind']}) "
                    f"has a SignalDelivery row — fan-out inserted it anyway. "
                    f"Trace's matched=False is wrong, OR fan-out filter is "
                    f"weaker than the trace claims."
                )

    async def test_trace_matched_groups_equal_fan_out_inserted_groups(
        self, db_session, metrics_client
    ):
        """Mirror of the user-side test for groups. Same invariants applied
        to `_match_groups` (SQL) vs `_trace_group` (Python)."""
        from etfpulse.models import TelegramGroup
        from etfpulse.pipeline.delivery import fan_out_signal

        sig = await _seed_signal(
            db_session,
            fp_seed="consistency-grp",
            status=SignalStatus.PENDING.value,
            confidence=7,
        )

        group_cases = [
            ("active_match", {}),
            ("inactive_group", {"is_active": False}),
            ("paused_group", {"pref_paused": True}),
            ("asset_mismatch", {"pref_assets": ["ETH"]}),
            ("confidence_too_high", {"pref_min_confidence": 9}),
        ]
        for idx, (_label, kwargs) in enumerate(group_cases):
            db_session.add(
                TelegramGroup(
                    chat_id=-100_000 - idx,
                    title=f"group-{idx}",
                    pref_assets=kwargs.get("pref_assets", ["BTC"]),
                    pref_min_confidence=kwargs.get("pref_min_confidence", 5),
                    is_active=kwargs.get("is_active", True),
                    pref_paused=kwargs.get("pref_paused", False),
                )
            )
        await db_session.flush()

        inserted = await fan_out_signal(db_session, sig.id)
        await db_session.flush()

        r = await metrics_client.get(
            f"/api/admin/signals/{sig.id}/delivery-trace",
            headers={"X-Admin-Key": "secret-key"},
        )
        body = r.json()
        group_recipients = [rec for rec in body["recipients"] if rec["kind"] == "group"]
        matched_groups = sum(1 for rec in group_recipients if rec["matched"])
        assert matched_groups == inserted

    async def test_market_signal_trace_mirrors_fan_out(self, db_session, metrics_client):
        """PR F.3 — pins the SQL/Python mirror for the MARKET branch.

        `_match_users` / `_match_groups` bypass `pref_assets` for MARKET
        signals; `_asset_matches` mirrors. If a future maintainer changes
        ONE side without the other, the matched-set/inserted-set invariant
        breaks. Without this case the existing consistency suite only
        exercises the single-asset branch.

        Seeds users + groups whose `pref_assets` would NORMALLY exclude
        a BTC signal (ETH-only / unrelated), plus the standard set of
        non-asset filters (paused, inactive, confidence floor) to confirm
        the MARKET branch only bypasses pref_assets, not the other rules.
        """
        from etfpulse.constants import MARKET_ASSET
        from etfpulse.pipeline.delivery import fan_out_signal

        sig = await _seed_signal(
            db_session,
            fp_seed="consistency-market",
            status=SignalStatus.PENDING.value,
            confidence=7,
            asset=MARKET_ASSET,
            signal_type="regime_shift",
        )

        # Each label encodes the expected match outcome. The MARKET branch
        # ignores pref_assets, so ETH-only and unrelated lists STILL match;
        # paused/inactive/confidence rules still apply.
        user_cases = [
            ("market_match_btc_prefs", {"pref_assets": ["BTC"]}, {}),
            ("market_match_eth_prefs", {"pref_assets": ["ETH"]}, {}),
            ("market_match_empty_prefs", {"pref_assets": []}, {}),
            ("market_excluded_paused", {"pref_paused": True}, {}),
            ("market_excluded_inactive_user", {"is_active": False}, {}),
            ("market_excluded_inactive_channel", {}, {"is_active": False}),
            ("market_excluded_confidence", {"pref_min_confidence": 9}, {}),
        ]
        for idx, (_label, u_kwargs, c_kwargs) in enumerate(user_cases):
            user = User(
                pref_assets=u_kwargs.get("pref_assets", ["BTC"]),
                pref_min_confidence=u_kwargs.get("pref_min_confidence", 5),
                is_active=u_kwargs.get("is_active", True),
                pref_paused=u_kwargs.get("pref_paused", False),
            )
            db_session.add(user)
            await db_session.flush()
            db_session.add(
                NotificationChannel(
                    user_id=user.id,
                    channel_type=ChannelType.TELEGRAM.value,
                    channel_identifier=f"market-consist-{idx}",
                    is_active=c_kwargs.get("is_active", True),
                )
            )

        # Group cases — same MARKET bypass logic must mirror on the group side.
        from etfpulse.models import TelegramGroup

        group_cases = [
            ("market_group_match_eth_prefs", {"pref_assets": ["ETH"]}),
            ("market_group_excluded_paused", {"pref_paused": True}),
            ("market_group_excluded_confidence", {"pref_min_confidence": 9}),
        ]
        for idx, (_label, kwargs) in enumerate(group_cases):
            db_session.add(
                TelegramGroup(
                    chat_id=-200_000 - idx,
                    title=f"market-group-{idx}",
                    pref_assets=kwargs.get("pref_assets", ["BTC"]),
                    pref_min_confidence=kwargs.get("pref_min_confidence", 5),
                    is_active=kwargs.get("is_active", True),
                    pref_paused=kwargs.get("pref_paused", False),
                )
            )
        await db_session.flush()

        inserted = await fan_out_signal(db_session, sig.id)
        await db_session.flush()

        r = await metrics_client.get(
            f"/api/admin/signals/{sig.id}/delivery-trace",
            headers={"X-Admin-Key": "secret-key"},
        )
        body = r.json()

        # Anti-drift invariant — matched set MUST equal inserted set.
        assert body["matched_count"] == inserted, (
            f"trace.matched_count={body['matched_count']} but fan-out "
            f"inserted={inserted} for MARKET signal; one side filters differently"
        )

        # Per-recipient pairing — same as the BTC consistency test.
        for rec in body["recipients"]:
            if rec["matched"]:
                assert rec["delivery_status"] is not None, (
                    f"matched MARKET recipient {rec['target_id']} ({rec['kind']}) "
                    f"has no SignalDelivery row"
                )
            else:
                assert rec["delivery_status"] is None, (
                    f"NON-matched MARKET recipient {rec['target_id']} ({rec['kind']}) "
                    f"has a SignalDelivery row — fan-out's MARKET branch is "
                    f"weaker than _asset_matches claims"
                )

        # Specifically pin the bypass — at least one ETH-prefs recipient
        # MUST be in the matched set. If pref_assets filtering ever
        # accidentally returns to MARKET signals, this assertion fails.
        eth_only_user_match = any(
            rec["matched"]
            for rec in body["recipients"]
            if rec["kind"] == "user" and "market-consist-1" in (rec["chat_id"] or "")
        )
        assert eth_only_user_match, (
            "MARKET signal must reach a user with pref_assets=['ETH'] — "
            "the bypass invariant is broken"
        )


class TestRetryAiRoute:
    """`POST /api/admin/signals/retry-ai` — auth gate + helper-orchestration
    contract. The helper itself is unit-tested in
    `tests/test_pipeline/test_ai_backfill.py`; here we just verify the
    route wires limit, key gate, error-sample passthrough, and commit
    correctly."""

    async def test_disabled_when_admin_key_empty(self, db_session, monkeypatch):
        monkeypatch.setattr(settings, "admin_api_key", "")
        app = create_app()

        async def _override() -> AsyncIterator:
            yield db_session

        app.dependency_overrides[get_db_session] = _override
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post("/api/admin/signals/retry-ai")
        app.dependency_overrides.clear()
        assert r.status_code == 503

    async def test_wrong_key_returns_401(self, metrics_client):
        r = await metrics_client.post(
            "/api/admin/signals/retry-ai", headers={"X-Admin-Key": "wrong"}
        )
        assert r.status_code == 401

    async def test_invalid_limit_returns_422(self, metrics_client):
        """Bounds enforced via FastAPI Query(ge=1, le=50). Below 1 → 422."""
        r = await metrics_client.post(
            "/api/admin/signals/retry-ai?limit=0",
            headers={"X-Admin-Key": "secret-key"},
        )
        assert r.status_code == 422

    async def test_limit_above_cap_returns_422(self, metrics_client):
        r = await metrics_client.post(
            "/api/admin/signals/retry-ai?limit=51",
            headers={"X-Admin-Key": "secret-key"},
        )
        assert r.status_code == 422

    async def test_route_passes_limit_through_and_returns_summary(
        self, metrics_client, monkeypatch
    ):
        """Route is a thin wrapper — its job is to forward `limit`, run
        the helper, return the typed summary. Stubbing the helper isolates
        the route's contract from the AI logic."""
        captured: dict = {}

        async def _stub(session, *, limit):
            captured["limit"] = limit
            return {
                "scanned": 3,
                "updated": 2,
                "failed": 1,
                "error_samples": [
                    {"signal_id": 99, "kind": "AnalyzeReturnedNone", "detail": "out of credits"}
                ],
            }

        monkeypatch.setattr("etfpulse.api.routes.admin.backfill_null_ai", _stub)

        r = await metrics_client.post(
            "/api/admin/signals/retry-ai?limit=7",
            headers={"X-Admin-Key": "secret-key"},
        )
        assert r.status_code == 200
        assert captured["limit"] == 7
        body = r.json()
        assert body["scanned"] == 3
        assert body["updated"] == 2
        assert body["failed"] == 1
        assert body["error_samples"] == [
            {"signal_id": 99, "kind": "AnalyzeReturnedNone", "detail": "out of credits"}
        ]

    async def test_default_limit_is_ten(self, metrics_client, monkeypatch):
        """Default `limit=10` — operator click without a query param caps
        OpenRouter spend at 10 calls."""
        captured: dict = {}

        async def _stub(session, *, limit):
            captured["limit"] = limit
            return {"scanned": 0, "updated": 0, "failed": 0, "error_samples": []}

        monkeypatch.setattr("etfpulse.api.routes.admin.backfill_null_ai", _stub)

        r = await metrics_client.post(
            "/api/admin/signals/retry-ai",
            headers={"X-Admin-Key": "secret-key"},
        )
        assert r.status_code == 200
        assert captured["limit"] == 10


class TestEvalOutcomesRoute:
    """`POST /api/admin/signals/eval-outcomes` — auth gate + the same
    helper-orchestration contract as Retry AI. The helper itself is
    unit-tested in `tests/test_pipeline/test_track_record.py`; here we
    just verify the route wires limit + key gate correctly."""

    _SAMPLE_SUMMARY = {
        "candidates": 3,
        "evaluated": 2,
        "skipped_no_direction": 1,
        "skipped_unknown_asset": 0,
        "skipped_no_klines": 0,
        "skipped_no_bars_in_window": 0,
        "errored": 0,
        "remaining": 5,
    }

    async def test_disabled_when_admin_key_empty(self, db_session, monkeypatch):
        monkeypatch.setattr(settings, "admin_api_key", "")
        app = create_app()

        async def _override() -> AsyncIterator:
            yield db_session

        app.dependency_overrides[get_db_session] = _override
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post("/api/admin/signals/eval-outcomes")
        app.dependency_overrides.clear()
        assert r.status_code == 503

    async def test_wrong_key_returns_401(self, metrics_client):
        r = await metrics_client.post(
            "/api/admin/signals/eval-outcomes", headers={"X-Admin-Key": "wrong"}
        )
        assert r.status_code == 401

    async def test_invalid_limit_returns_422(self, metrics_client):
        """Bounds [1, 100] enforced via Query. Operator can't drain
        thousands in one click."""
        r = await metrics_client.post(
            "/api/admin/signals/eval-outcomes?limit=0",
            headers={"X-Admin-Key": "secret-key"},
        )
        assert r.status_code == 422

    async def test_limit_above_cap_returns_422(self, metrics_client):
        r = await metrics_client.post(
            "/api/admin/signals/eval-outcomes?limit=101",
            headers={"X-Admin-Key": "secret-key"},
        )
        assert r.status_code == 422

    async def test_route_passes_limit_through_and_returns_summary(
        self, metrics_client, monkeypatch
    ):
        """Route forwards `limit` to the helper and returns the typed summary."""
        captured: dict = {}

        async def _stub(session, *, limit):
            captured["limit"] = limit
            return self._SAMPLE_SUMMARY

        monkeypatch.setattr("etfpulse.api.routes.admin.evaluate_pending_outcomes", _stub)

        r = await metrics_client.post(
            "/api/admin/signals/eval-outcomes?limit=15",
            headers={"X-Admin-Key": "secret-key"},
        )
        assert r.status_code == 200
        assert captured["limit"] == 15
        body = r.json()
        assert body["evaluated"] == 2
        assert body["remaining"] == 5
        assert body["skipped_no_direction"] == 1

    async def test_default_limit_is_twenty(self, metrics_client, monkeypatch):
        """Default `limit=20` — smaller than the scheduled job's 50 cap
        so a click can't accidentally fire 50+ klines requests."""
        captured: dict = {}

        async def _stub(session, *, limit):
            captured["limit"] = limit
            return self._SAMPLE_SUMMARY

        monkeypatch.setattr("etfpulse.api.routes.admin.evaluate_pending_outcomes", _stub)

        r = await metrics_client.post(
            "/api/admin/signals/eval-outcomes",
            headers={"X-Admin-Key": "secret-key"},
        )
        assert r.status_code == 200
        assert captured["limit"] == 20
