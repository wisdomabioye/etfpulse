"""signal_builder integration tests against the test DB.

We test orchestration, not detector logic — `ALL_DETECTORS` is monkey-patched
to a list of stubs so test outcomes don't depend on whether the SoSoValue
fixture data happens to trigger a real detector.

The five required cases (a–e from #44) are each a separate test below.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select

from etfpulse.models import Signal
from etfpulse.pipeline.analysis import AISignalAnalysis
from etfpulse.pipeline.detectors import DetectorHit, compute_fingerprint
from etfpulse.pipeline.signal_builder import build_signal, run_daily_cycle

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_hit(
    *,
    signal_type: str = "flow_anomaly",
    asset: str = "BTC",
    signal_date: date = date(2026, 4, 22),
    extra: str = "v1",
) -> DetectorHit:
    """Build a synthetic DetectorHit with a deterministic, unique fingerprint."""
    return DetectorHit(
        signal_type=signal_type,
        asset=asset,
        signal_date=signal_date,
        trigger_data={"streak_length": 3, "streak_direction": "long"},
        fingerprint=compute_fingerprint(signal_type, asset, signal_date.isoformat(), extra),
    )


_VALID_ANALYSIS = AISignalAnalysis(
    headline="Test signal",
    reasoning=["r1"],
    confidence=8,
    risks=["risk1"],
    suggested_action="consider short",
    time_horizon="swing",
)


class _StubDetector:
    """In-memory detector that returns a fixed list of hits.

    Use this in `monkeypatch.setattr(signal_builder, "ALL_DETECTORS", ...)`
    to make `run_daily_cycle` deterministic.
    """

    def __init__(self, name: str, hits: list[DetectorHit]) -> None:
        self.name = name
        self.signal_type = hits[0].signal_type if hits else "flow_anomaly"
        self._hits = hits

    async def detect(self, session):  # type: ignore[no-untyped-def]
        return list(self._hits)


class _BrokenDetector:
    name = "broken"
    signal_type = "flow_anomaly"

    async def detect(self, session):  # type: ignore[no-untyped-def]
        raise RuntimeError("boom")


# ---------------------------------------------------------------------------
# build_signal — direct tests
# ---------------------------------------------------------------------------


class TestBuildSignal:
    async def test_inserts_with_ai_analysis(self, db_session, monkeypatch):
        """Happy path — AI returns valid analysis, all fields populated."""

        async def _ai(*args, **kwargs):
            return _VALID_ANALYSIS

        monkeypatch.setattr("etfpulse.pipeline.signal_builder.openrouter_client.analyze", _ai)

        hit = _make_hit()
        signal = await build_signal(db_session, hit)

        assert signal is not None
        assert signal.id is not None
        assert signal.signal_type == "flow_anomaly"
        assert signal.asset == "BTC"
        assert signal.signal_date == date(2026, 4, 22)
        assert signal.confidence == 8
        assert signal.ai_analysis is not None
        assert signal.ai_analysis["headline"] == "Test signal"
        assert signal.expires_at is not None
        # Default param — price fields stay NULL when the caller passes nothing.
        # The real production path (`run_daily_cycle`) threads a price through
        # via `pipeline.prices`; see `test_builds_with_price_and_source` below.
        assert signal.price_at_creation is None

    async def test_inserts_without_ai_when_analyze_returns_none(self, db_session, monkeypatch):
        """R6 — AI failure must not block signal persistence."""

        async def _ai(*args, **kwargs):
            return None

        monkeypatch.setattr("etfpulse.pipeline.signal_builder.openrouter_client.analyze", _ai)

        hit = _make_hit()
        signal = await build_signal(db_session, hit)

        assert signal is not None
        assert signal.id is not None
        assert signal.ai_analysis is None
        assert signal.confidence is None
        assert signal.expires_at is None

    async def test_idempotent_returns_none_on_duplicate(self, db_session, monkeypatch):
        """Second insert of same fingerprint+date is a silent no-op."""

        async def _ai(*args, **kwargs):
            return _VALID_ANALYSIS

        monkeypatch.setattr("etfpulse.pipeline.signal_builder.openrouter_client.analyze", _ai)

        hit = _make_hit()
        first = await build_signal(db_session, hit)
        second = await build_signal(db_session, hit)

        assert first is not None
        assert second is None
        # Only one row exists in the table.
        rows = (await db_session.execute(select(Signal))).scalars().all()
        assert len(rows) == 1

    async def test_idempotent_skip_does_not_call_ai(self, db_session, monkeypatch):
        """D12 — re-runs of the same hit must NOT spend an OpenRouter call."""
        call_count = 0

        async def _ai(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return _VALID_ANALYSIS

        monkeypatch.setattr("etfpulse.pipeline.signal_builder.openrouter_client.analyze", _ai)

        hit = _make_hit()
        await build_signal(db_session, hit)
        await build_signal(db_session, hit)

        assert call_count == 1, "AI must be called exactly once across two identical hits"

    async def test_different_signal_date_creates_separate_row(self, db_session, monkeypatch):
        """The unique index includes signal_date — same fingerprint on a
        new date is a different signal."""

        async def _ai(*args, **kwargs):
            return _VALID_ANALYSIS

        monkeypatch.setattr("etfpulse.pipeline.signal_builder.openrouter_client.analyze", _ai)

        # Same fingerprint via same `extra`, but different signal_date.
        hit_a = _make_hit(signal_date=date(2026, 4, 22), extra="same")
        hit_b = _make_hit(signal_date=date(2026, 4, 23), extra="same")
        # Sanity: fingerprints DO match (we built them with the same extras
        # but different signal_dates means the date is what disambiguates).
        # Actually our fingerprint helper includes signal_date in the hash —
        # check that, then make a hit pair with the SAME fingerprint to
        # really test the (fingerprint, signal_date) compound uniqueness.
        forced_fp = compute_fingerprint("force", "same")
        hit_a = DetectorHit(
            signal_type="flow_anomaly",
            asset="BTC",
            signal_date=date(2026, 4, 22),
            trigger_data={},
            fingerprint=forced_fp,
        )
        hit_b = DetectorHit(
            signal_type="flow_anomaly",
            asset="BTC",
            signal_date=date(2026, 4, 23),
            trigger_data={},
            fingerprint=forced_fp,
        )

        a = await build_signal(db_session, hit_a)
        b = await build_signal(db_session, hit_b)

        assert a is not None and b is not None
        assert a.id != b.id
        rows = (await db_session.execute(select(Signal))).scalars().all()
        assert len(rows) == 2

    async def test_builds_with_price_and_source(self, db_session, monkeypatch, stub_ai):
        """Issue #34: caller passes price + source → row stores both.

        Price lands on `Signal.price_at_creation` (typed Numeric, Decimal in
        Python). Source is stuffed into `trigger_data` as a JSONB field so
        Stage 08 outcome evaluation can pin +24h/+72h lookups to the same
        provider without a schema migration.
        """
        from decimal import Decimal

        hit = _make_hit()
        signal = await build_signal(
            db_session,
            hit,
            price_at_creation=Decimal("84120.50"),
            price_source="sosovalue",
        )
        assert signal is not None
        assert signal.price_at_creation == Decimal("84120.50")
        assert signal.trigger_data["price_source"] == "sosovalue"
        # Original trigger_data keys preserved — we don't stomp on them.
        for key in hit.trigger_data:
            assert signal.trigger_data[key] == hit.trigger_data[key]

    async def test_builds_without_price_leaves_fields_null(
        self, db_session, monkeypatch, stub_ai
    ):
        """When both price providers fail, the signal still persists — with
        NULL `price_at_creation` and no `price_source` tag. The backfill
        script (scripts/backfill_signal_prices.py) is responsible for
        revisiting these rows later."""
        hit = _make_hit()
        signal = await build_signal(db_session, hit)  # both price args default None
        assert signal is not None
        assert signal.price_at_creation is None
        assert "price_source" not in signal.trigger_data


# ---------------------------------------------------------------------------
# run_daily_cycle — orchestrator tests
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_ai(monkeypatch):
    """Make OpenRouter return a valid analysis for every call."""

    async def _ai(*args, **kwargs):
        return _VALID_ANALYSIS

    monkeypatch.setattr("etfpulse.pipeline.signal_builder.openrouter_client.analyze", _ai)


class TestRunDailyCycle:
    async def test_first_call_inserts_hits(self, db_session, monkeypatch, stub_ai):
        """Case (a): ALL_DETECTORS yields hits → signals_new > 0."""
        stub = _StubDetector(
            "stub",
            [_make_hit(asset="BTC", extra="run1"), _make_hit(asset="ETH", extra="run1")],
        )
        monkeypatch.setattr("etfpulse.pipeline.signal_builder.ALL_DETECTORS", [stub])

        summary = await run_daily_cycle(db_session)

        assert summary["signals_new"] == 2
        assert summary["signals_duplicate"] == 0
        assert summary["detector_errors"] == []
        rows = (await db_session.execute(select(Signal))).scalars().all()
        assert len(rows) == 2

    async def test_idempotent_rerun_yields_zero_new(self, db_session, monkeypatch, stub_ai):
        """Case (b): same cycle run twice → second run reports 0 new, all duplicate."""
        stub = _StubDetector("stub", [_make_hit(extra="run2")])
        monkeypatch.setattr("etfpulse.pipeline.signal_builder.ALL_DETECTORS", [stub])

        first = await run_daily_cycle(db_session)
        second = await run_daily_cycle(db_session)

        assert first["signals_new"] == 1
        assert second["signals_new"] == 0
        assert second["signals_duplicate"] == 1

    async def test_ai_failure_persists_signal_with_null_analysis(self, db_session, monkeypatch):
        """Case (c): AI returns None → signal exists, ai_analysis is NULL."""

        async def _ai_none(*args, **kwargs):
            return None

        monkeypatch.setattr("etfpulse.pipeline.signal_builder.openrouter_client.analyze", _ai_none)
        stub = _StubDetector("stub", [_make_hit(extra="run3")])
        monkeypatch.setattr("etfpulse.pipeline.signal_builder.ALL_DETECTORS", [stub])

        summary = await run_daily_cycle(db_session)

        assert summary["signals_new"] == 1
        assert summary["ai_succeeded"] == 0
        assert summary["ai_failed"] == 1
        signal = (await db_session.execute(select(Signal))).scalar_one()
        assert signal.ai_analysis is None
        assert signal.confidence is None

    async def test_different_signal_date_yields_separate_rows(
        self, db_session, monkeypatch, stub_ai
    ):
        """Case (d): two stubs hitting different signal_dates produce two rows."""
        stub1 = _StubDetector("stub1", [_make_hit(signal_date=date(2026, 4, 22), extra="d1")])
        stub2 = _StubDetector("stub2", [_make_hit(signal_date=date(2026, 4, 23), extra="d2")])
        monkeypatch.setattr("etfpulse.pipeline.signal_builder.ALL_DETECTORS", [stub1, stub2])

        summary = await run_daily_cycle(db_session)

        assert summary["signals_new"] == 2
        rows = (await db_session.execute(select(Signal))).scalars().all()
        dates = {r.signal_date for r in rows}
        assert dates == {date(2026, 4, 22), date(2026, 4, 23)}

    async def test_detector_exception_does_not_kill_cycle(self, db_session, monkeypatch, stub_ai):
        """Case (e): one detector raises, the next still runs (D13)."""
        broken = _BrokenDetector()
        good = _StubDetector("good", [_make_hit(extra="surv")])
        monkeypatch.setattr("etfpulse.pipeline.signal_builder.ALL_DETECTORS", [broken, good])

        summary = await run_daily_cycle(db_session)

        assert summary["detector_errors"] == [("broken", "RuntimeError")]
        assert summary["signals_new"] == 1
        # The good detector's hit still landed.
        rows = (await db_session.execute(select(Signal))).scalars().all()
        assert len(rows) == 1

    async def test_summary_shape(self, db_session, monkeypatch, stub_ai):
        """The returned dict has the documented keys — protects #47/#50 callers."""
        monkeypatch.setattr("etfpulse.pipeline.signal_builder.ALL_DETECTORS", [])

        summary = await run_daily_cycle(db_session)

        expected_keys = {
            "ingested",
            "ingest_errors",
            "prices",
            "price_errors",
            "detectors_run",
            "detector_errors",
            "signals_new",
            "signals_duplicate",
            "ai_succeeded",
            "ai_failed",
        }
        assert set(summary.keys()) == expected_keys
