"""MagnitudeDetector — pure-function tests.

Builds `(date, Decimal)` sequences inline rather than leaning on fixtures —
the BTC fixture's 7-day history is too short to exercise an 80th-percentile
threshold over 90 days anyway.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from etfpulse.pipeline.detectors.magnitude import MagnitudeDetector


def _seq(*flows: int | float, start: date = date(2026, 1, 1)) -> list[tuple[date, Decimal]]:
    return [(start + timedelta(days=i), Decimal(str(f))) for i, f in enumerate(flows)]


@pytest.fixture
def detector() -> MagnitudeDetector:
    # Defaults: lookback=90, p80, min_history=30.
    return MagnitudeDetector()


class TestMagnitudeDetection:
    def test_insufficient_history_returns_none(self, detector: MagnitudeDetector):
        # Only 10 days — below min_history_days=30, so no percentile is stable.
        rows = _seq(*([100] * 9 + [10000]))
        assert detector._detect_magnitude("BTC", rows) is None

    def test_flat_history_no_hit(self, detector: MagnitudeDetector):
        # 90 days all equal — latest is NOT strictly greater than p80.
        rows = _seq(*([100] * 90))
        assert detector._detect_magnitude("BTC", rows) is None

    def test_extreme_outlier_emits_long_hit(self, detector: MagnitudeDetector):
        # 89 flat days + 1 huge positive spike on the most recent day.
        rows = _seq(*([100] * 89 + [10000]))
        hit = detector._detect_magnitude("BTC", rows)
        assert hit is not None
        assert hit.asset == "BTC"
        assert hit.trigger_data["direction"] == "long"
        assert Decimal(hit.trigger_data["abs_flow_usd"]) == Decimal("10000")
        assert hit.signal_date == rows[-1][0]

    def test_extreme_outlier_emits_short_hit(self, detector: MagnitudeDetector):
        # Symmetric — big negative = short-direction magnitude signal.
        rows = _seq(*([100] * 89 + [-10000]))
        hit = detector._detect_magnitude("BTC", rows)
        assert hit is not None
        assert hit.trigger_data["direction"] == "short"

    def test_below_threshold_no_hit(self, detector: MagnitudeDetector):
        # 89 days spread 0-100 + latest=120 — above p80 typically, but with
        # this distribution the p80 ~= 80, so 120 > 80 → hit.
        # To test BELOW threshold, make almost all days at 100 and latest=80.
        rows = _seq(*([100] * 89 + [80]))
        assert detector._detect_magnitude("BTC", rows) is None

    def test_zero_on_latest_day_no_hit(self, detector: MagnitudeDetector):
        # Zero has no direction to report — skip rather than guess.
        rows = _seq(*([100] * 89 + [0]))
        assert detector._detect_magnitude("BTC", rows) is None

    def test_lookback_honored_via_constructor(self):
        # Narrower window + lower min_history lets us test on smaller seqs.
        narrow = MagnitudeDetector(lookback_days=30, percentile_threshold=0.80, min_history_days=10)
        rows = _seq(*([100] * 29 + [5000]))
        hit = narrow._detect_magnitude("BTC", rows)
        assert hit is not None


class TestMagnitudeFingerprint:
    def test_deterministic(self, detector: MagnitudeDetector):
        rows = _seq(*([100] * 89 + [10000]))
        a = detector._detect_magnitude("BTC", rows)
        b = detector._detect_magnitude("BTC", rows)
        assert a is not None and b is not None
        assert a.fingerprint == b.fingerprint

    def test_direction_flips_fingerprint(self, detector: MagnitudeDetector):
        long_rows = _seq(*([100] * 89 + [10000]))
        short_rows = _seq(*([100] * 89 + [-10000]))
        long_hit = detector._detect_magnitude("BTC", long_rows)
        short_hit = detector._detect_magnitude("BTC", short_rows)
        assert long_hit is not None and short_hit is not None
        assert long_hit.fingerprint != short_hit.fingerprint

    def test_threshold_does_not_affect_fingerprint(self, detector: MagnitudeDetector):
        """Per #51 spec — fingerprint is (asset|magnitude|date|direction) only.
        A detector with a looser threshold that hits the SAME date + direction
        must produce the SAME fingerprint so a backfill-induced re-run doesn't
        double-fire."""
        loose = MagnitudeDetector(lookback_days=90, percentile_threshold=0.50, min_history_days=30)
        strict = MagnitudeDetector(lookback_days=90, percentile_threshold=0.80, min_history_days=30)
        rows = _seq(*([100] * 89 + [10000]))
        a = loose._detect_magnitude("BTC", rows)
        b = strict._detect_magnitude("BTC", rows)
        assert a is not None and b is not None
        assert a.fingerprint == b.fingerprint


class TestMagnitudeDBIntegration:
    async def test_detect_against_db(self, db_session):
        from etfpulse.models import ETFFlow

        # 30 flat days + 1 outlier (hits min_history threshold).
        for i in range(30):
            db_session.add(
                ETFFlow(
                    asset="BTC",
                    captured_at=date(2026, 1, 1) + timedelta(days=i),
                    total_net_flow_usd=Decimal("100"),
                )
            )
        db_session.add(
            ETFFlow(
                asset="BTC",
                captured_at=date(2026, 1, 31),
                total_net_flow_usd=Decimal("100000"),
            )
        )
        await db_session.flush()

        # Narrow lookback so this synthetic dataset fits.
        detector = MagnitudeDetector(
            lookback_days=40, percentile_threshold=0.80, min_history_days=20
        )
        hits = await detector.detect(db_session)
        assert len(hits) == 1
        assert hits[0].asset == "BTC"
        assert hits[0].trigger_data["direction"] == "long"
