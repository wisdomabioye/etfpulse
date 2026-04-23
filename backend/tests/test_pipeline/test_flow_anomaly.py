"""FlowAnomalyDetector — pure-function tests over `_detect_streak_break`.

We test the pure function directly with hand-built `(date, Decimal)` tuples
rather than going through the DB. The DB layer in `detect()` is a thin
`select(...).order_by(...).limit(...)` wrapper that's covered by an
integration-style smoke test below.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from etfpulse.pipeline.detectors.flow_anomaly import FlowAnomalyDetector


def _seq(*flows: int | float, start: date = date(2026, 4, 1)) -> list[tuple[date, Decimal]]:
    """Build a list of (consecutive-date, Decimal) tuples ascending."""
    return [(start + timedelta(days=i), Decimal(str(f))) for i, f in enumerate(flows)]


@pytest.fixture
def detector() -> FlowAnomalyDetector:
    # Defaults: lookback_days=14, min_streak_length=3.
    return FlowAnomalyDetector()


class TestStreakBreakDetection:
    def test_no_data_returns_none(self, detector: FlowAnomalyDetector):
        assert detector._detect_streak_break("BTC", []) is None

    def test_single_day_returns_none(self, detector: FlowAnomalyDetector):
        assert detector._detect_streak_break("BTC", _seq(100)) is None

    def test_active_streak_no_break_returns_none(self, detector: FlowAnomalyDetector):
        # All inflows, no flip — trend is intact.
        rows = _seq(100, 200, 300, 400, 500)
        assert detector._detect_streak_break("BTC", rows) is None

    def test_streak_broken_emits_hit(self, detector: FlowAnomalyDetector):
        # 3 days of inflows then an outflow — classic long-trend reversal.
        rows = _seq(100, 200, 300, -50)
        hit = detector._detect_streak_break("BTC", rows)
        assert hit is not None
        assert hit.asset == "BTC"
        assert hit.signal_date == date(2026, 4, 4)  # the break day
        assert hit.trigger_data["streak_length"] == 3
        assert hit.trigger_data["streak_direction"] == "long"
        assert hit.trigger_data["break_flow_usd"] == "-50"
        assert hit.trigger_data["streak_flows_usd"] == ["100", "200", "300"]
        assert len(hit.fingerprint) == 32

    def test_short_streak_broken_by_long(self, detector: FlowAnomalyDetector):
        # Symmetric case — short trend reversed by long.
        rows = _seq(-100, -200, -300, 50)
        hit = detector._detect_streak_break("BTC", rows)
        assert hit is not None
        assert hit.trigger_data["streak_direction"] == "short"
        assert hit.trigger_data["break_flow_usd"] == "50"

    def test_streak_below_min_length_no_hit(self, detector: FlowAnomalyDetector):
        # 2-day streak isn't long enough to "break".
        rows = _seq(100, 200, -50)
        assert detector._detect_streak_break("BTC", rows) is None

    def test_streak_then_reform_hits_only_on_break(self, detector: FlowAnomalyDetector):
        # 3 long, 1 short break, then 3 more long. If "today" is the latest
        # long day, the most recent isn't a break — no hit.
        rows = _seq(100, 200, 300, -50, 100, 200, 300)
        assert detector._detect_streak_break("BTC", rows) is None

        # But if the data ends on the break day, we DO hit. (Catch-up
        # logic in #45 re-runs missed days as if it were that date.)
        rows_ending_on_break = _seq(100, 200, 300, -50)
        assert detector._detect_streak_break("BTC", rows_ending_on_break) is not None

    def test_zero_flow_days_skipped(self, detector: FlowAnomalyDetector):
        # Zeros are treated as if the day didn't exist. The sequence below
        # collapses to [+, +, +, -] under the zero-skip rule → 3-streak break.
        rows = _seq(100, 200, 0, 300, -50)
        hit = detector._detect_streak_break("BTC", rows)
        assert hit is not None
        assert hit.trigger_data["streak_length"] == 3

    def test_zero_on_break_day_no_hit(self, detector: FlowAnomalyDetector):
        # If the most recent day is a zero, it's skipped; the day before it
        # becomes the candidate break. With [+, +, +, +, 0] the candidate
        # is the 4th + day — same direction as the prior streak — no hit.
        rows = _seq(100, 200, 300, 400, 0)
        assert detector._detect_streak_break("BTC", rows) is None


class TestFingerprintDeterminism:
    def test_same_input_same_fingerprint(self, detector: FlowAnomalyDetector):
        rows = _seq(100, 200, 300, -50)
        a = detector._detect_streak_break("BTC", rows)
        b = detector._detect_streak_break("BTC", rows)
        assert a is not None and b is not None
        assert a.fingerprint == b.fingerprint

    def test_different_asset_different_fingerprint(self, detector: FlowAnomalyDetector):
        rows = _seq(100, 200, 300, -50)
        btc = detector._detect_streak_break("BTC", rows)
        eth = detector._detect_streak_break("ETH", rows)
        assert btc is not None and eth is not None
        assert btc.fingerprint != eth.fingerprint

    def test_different_streak_length_different_fingerprint(self, detector: FlowAnomalyDetector):
        # Streak of 3 vs streak of 4, same break date — different fingerprints
        # so we don't dedupe across genuinely different events.
        rows3 = _seq(100, 200, 300, -50)
        rows4 = _seq(100, 200, 300, 400, -50)
        a = detector._detect_streak_break("BTC", rows3)
        b = detector._detect_streak_break("BTC", rows4)
        assert a is not None and b is not None
        assert a.trigger_data["streak_length"] != b.trigger_data["streak_length"]
        # break_date differs too (rows4 is one day longer), so they'd dedupe
        # via signal_date; check that the fingerprint reflects the length too.
        assert a.fingerprint != b.fingerprint


class TestConfigurableThresholds:
    def test_custom_min_streak_length(self):
        # min_streak_length=2 lowers the bar — a 2-day streak now counts.
        detector = FlowAnomalyDetector(min_streak_length=2)
        rows = _seq(100, 200, -50)
        hit = detector._detect_streak_break("BTC", rows)
        assert hit is not None
        assert hit.trigger_data["streak_length"] == 2

    def test_lookback_days_attribute_honored(self):
        # `lookback_days` is the SQL `LIMIT` — verified via attribute,
        # the DB-layer integration test exercises the query path.
        detector = FlowAnomalyDetector(lookback_days=7)
        assert detector.lookback_days == 7


class TestDetectIntegration:
    """Thin DB-layer test — the SQL ordering + reverse logic is the only
    thing the unit tests can't exercise."""

    async def test_detect_against_db(self, db_session):
        from etfpulse.models import ETFFlow

        # Seed a 4-day streak break for BTC; ETH gets nothing → no hit for it.
        for i, flow in enumerate([100, 200, 300, -50]):
            db_session.add(
                ETFFlow(
                    asset="BTC",
                    captured_at=date(2026, 4, 1) + timedelta(days=i),
                    total_net_flow_usd=Decimal(str(flow)),
                )
            )
        await db_session.flush()

        hits = await FlowAnomalyDetector().detect(db_session)
        assert len(hits) == 1
        assert hits[0].asset == "BTC"
        assert hits[0].signal_date == date(2026, 4, 4)
        assert hits[0].trigger_data["streak_length"] == 3
