"""AccelerationDetector — pure-function tests.

Same hand-built-sequence pattern as flow_anomaly / magnitude. A 7-day
window means we need at least 14 rows to fire.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from etfpulse.pipeline.detectors.acceleration import AccelerationDetector


def _seq(*flows: int | float, start: date = date(2026, 4, 1)) -> list[tuple[date, Decimal]]:
    return [(start + timedelta(days=i), Decimal(str(f))) for i, f in enumerate(flows)]


@pytest.fixture
def detector() -> AccelerationDetector:
    # Defaults: window=7, threshold=0.5, min_prior_usd=$1M.
    return AccelerationDetector()


class TestAccelerationDetection:
    def test_insufficient_data_returns_none(self, detector: AccelerationDetector):
        # Window=7 needs 14 rows minimum.
        rows = _seq(*([1_000_000] * 13))
        assert detector._detect_acceleration("BTC", rows) is None

    def test_flat_flow_no_hit(self, detector: AccelerationDetector):
        # 14 identical days — prior and recent sums are equal → change = 0.
        rows = _seq(*([1_000_000] * 14))
        assert detector._detect_acceleration("BTC", rows) is None

    def test_strong_positive_acceleration_long_hit(self, detector: AccelerationDetector):
        # Prior 7d sum = 7M, recent 7d sum = 14M → change = 1.0 (100% increase).
        rows = _seq(*([1_000_000] * 7 + [2_000_000] * 7))
        hit = detector._detect_acceleration("BTC", rows)
        assert hit is not None
        assert hit.asset == "BTC"
        assert hit.trigger_data["direction"] == "long"
        assert Decimal(hit.trigger_data["change_ratio"]) == Decimal("1")
        assert hit.signal_date == rows[-1][0]

    def test_strong_negative_acceleration_short_hit(self, detector: AccelerationDetector):
        # Prior positive, recent negative — direction = "short" (sign of recent).
        rows = _seq(*([2_000_000] * 7 + [-1_000_000] * 7))
        hit = detector._detect_acceleration("BTC", rows)
        assert hit is not None
        assert hit.trigger_data["direction"] == "short"

    def test_below_threshold_no_hit(self, detector: AccelerationDetector):
        # Recent 20% above prior → change = 0.2, below 0.5 threshold.
        rows = _seq(*([1_000_000] * 7 + [1_200_000] * 7))
        assert detector._detect_acceleration("BTC", rows) is None

    def test_small_prior_sum_skipped(self, detector: AccelerationDetector):
        # Prior 7d sum = $700 — way below $1M floor. Must NOT divide by near-zero.
        rows = _seq(*([100] * 7 + [10_000_000] * 7))
        assert detector._detect_acceleration("BTC", rows) is None

    def test_negative_prior_with_positive_recent(self, detector: AccelerationDetector):
        # Prior sum = -7M, recent = +7M → change = (7M - (-7M)) / -7M = -2.0.
        # |change| = 2.0 >= 0.5 → hit. Direction follows recent sign → "long".
        rows = _seq(*([-1_000_000] * 7 + [1_000_000] * 7))
        hit = detector._detect_acceleration("BTC", rows)
        assert hit is not None
        assert hit.trigger_data["direction"] == "long"
        # Change ratio is computed; sign reflects the division.
        assert abs(Decimal(hit.trigger_data["change_ratio"])) >= Decimal("0.5")


class TestAccelerationFingerprint:
    def test_deterministic(self, detector: AccelerationDetector):
        rows = _seq(*([1_000_000] * 7 + [2_000_000] * 7))
        a = detector._detect_acceleration("BTC", rows)
        b = detector._detect_acceleration("BTC", rows)
        assert a is not None and b is not None
        assert a.fingerprint == b.fingerprint

    def test_direction_flips_fingerprint(self, detector: AccelerationDetector):
        long_rows = _seq(*([1_000_000] * 7 + [2_000_000] * 7))
        short_rows = _seq(*([2_000_000] * 7 + [-1_000_000] * 7))
        long_hit = detector._detect_acceleration("BTC", long_rows)
        short_hit = detector._detect_acceleration("BTC", short_rows)
        assert long_hit is not None and short_hit is not None
        assert long_hit.fingerprint != short_hit.fingerprint

    def test_threshold_does_not_affect_fingerprint(self):
        """Same guarantee as MagnitudeDetector — tuning the threshold must
        not change the fingerprint for the same (asset, date, direction)."""
        loose = AccelerationDetector(window=7, change_threshold=0.3)
        strict = AccelerationDetector(window=7, change_threshold=0.5)
        rows = _seq(*([1_000_000] * 7 + [2_000_000] * 7))
        a = loose._detect_acceleration("BTC", rows)
        b = strict._detect_acceleration("BTC", rows)
        assert a is not None and b is not None
        assert a.fingerprint == b.fingerprint


class TestAccelerationDBIntegration:
    async def test_detect_against_db(self, db_session):
        from etfpulse.models import ETFFlow

        # Prior 7 days at $1M, recent 7 days at $2M → 100% acceleration.
        start = date(2026, 4, 1)
        for i in range(7):
            db_session.add(
                ETFFlow(
                    asset="BTC",
                    captured_at=start + timedelta(days=i),
                    total_net_flow_usd=Decimal("1000000"),
                )
            )
        for i in range(7, 14):
            db_session.add(
                ETFFlow(
                    asset="BTC",
                    captured_at=start + timedelta(days=i),
                    total_net_flow_usd=Decimal("2000000"),
                )
            )
        await db_session.flush()

        hits = await AccelerationDetector().detect(db_session)
        assert len(hits) == 1
        assert hits[0].asset == "BTC"
        assert hits[0].trigger_data["direction"] == "long"
