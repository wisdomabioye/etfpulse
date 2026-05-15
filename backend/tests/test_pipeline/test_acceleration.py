"""AccelerationDetector — pure-function tests.

PR F.1 — rewritten for the true-second-derivative algorithm (three adjacent
7-day windows, threshold 100% by default). Linear growth no longer fires;
only changes in the slope itself do.
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
    # Defaults: window=7 (so 21 rows needed), threshold=1.00, min_prior_usd=$1M.
    return AccelerationDetector()


class TestAccelerationDetection:
    def test_insufficient_data_returns_none(self, detector: AccelerationDetector):
        """Window=7 needs `window * 3` = 21 rows minimum under the new
        second-derivative algorithm."""
        rows = _seq(*([1_000_000] * 20))
        assert detector._detect_acceleration("BTC", rows) is None

    def test_flat_flow_no_hit(self, detector: AccelerationDetector):
        """21 identical days — all three window sums equal → slope_old = 0
        → blocked by the slope floor BEFORE we reach the threshold check."""
        rows = _seq(*([1_000_000] * 21))
        assert detector._detect_acceleration("BTC", rows) is None

    def test_linear_growth_does_not_fire(self, detector: AccelerationDetector):
        """The KEY regression vs the pre-F.1 detector: steady linear growth
        is NOT acceleration. Each window's sum is bigger than the prior, but
        the SLOPE between sums is constant → second derivative = 0 → no hit.

        Pre-F.1 this would have fired on the recent-vs-prior 50% threshold.
        """
        # 7d at 1M (sum=7M), 7d at 2M (sum=14M), 7d at 3M (sum=21M).
        # slope_old = 14M - 7M = +7M
        # slope_new = 21M - 14M = +7M
        # second_derivative = 7M - 7M = 0 → no hit
        rows = _seq(*([1_000_000] * 7 + [2_000_000] * 7 + [3_000_000] * 7))
        assert detector._detect_acceleration("BTC", rows) is None

    def test_strong_positive_acceleration_long_hit(self, detector: AccelerationDetector):
        """Slope going from $7M/week to $14M/week — second derivative is
        +$7M, ratio = 7M/7M = 1.00 → exactly at the 100% threshold (inclusive)."""
        # 7d at 0 (sum=0), 7d at 1M (sum=7M), 7d at 3M (sum=21M).
        # slope_old = 7M - 0 = +7M
        # slope_new = 21M - 7M = +14M
        # second_derivative = +7M → change_ratio = 7M/7M = 1.0 → fire long
        rows = _seq(*([0] * 7 + [1_000_000] * 7 + [3_000_000] * 7))
        hit = detector._detect_acceleration("BTC", rows)
        assert hit is not None
        assert hit.asset == "BTC"
        assert hit.trigger_data["direction"] == "long"
        assert Decimal(hit.trigger_data["second_derivative_usd"]) == Decimal("7000000")
        assert Decimal(hit.trigger_data["change_ratio"]) == Decimal("1")
        assert hit.signal_date == rows[-1][0]

    def test_strong_negative_acceleration_short_hit(self, detector: AccelerationDetector):
        """Trend decelerating from +$7M/week to -$7M/week — second derivative
        flips negative → fires SHORT."""
        # 7d at 0 (sum=0), 7d at 1M (sum=7M), 7d at 0 (sum=0).
        # slope_old = 7M - 0 = +7M
        # slope_new = 0 - 7M = -7M
        # second_derivative = -14M → change_ratio = -14M/7M = -2.0 → fire short
        rows = _seq(*([0] * 7 + [1_000_000] * 7 + [0] * 7))
        hit = detector._detect_acceleration("BTC", rows)
        assert hit is not None
        assert hit.trigger_data["direction"] == "short"
        assert Decimal(hit.trigger_data["second_derivative_usd"]) == Decimal("-14000000")
        assert Decimal(hit.trigger_data["change_ratio"]) == Decimal("-2")

    def test_below_threshold_no_hit(self, detector: AccelerationDetector):
        """Slope changes by 50% — below the 100% default threshold → no hit."""
        # 7d at 0 (sum=0), 7d at 1M (sum=7M), 7d at 1.5M (sum=10.5M).
        # slope_old = 7M - 0 = +7M
        # slope_new = 10.5M - 7M = +3.5M
        # second_derivative = -3.5M → change_ratio = -3.5M/7M = -0.5
        # |0.5| < 1.0 threshold → no hit
        rows = _seq(*([0] * 7 + [1_000_000] * 7 + [1_500_000] * 7))
        assert detector._detect_acceleration("BTC", rows) is None

    def test_small_slope_old_skipped(self, detector: AccelerationDetector):
        """|slope_old| < $1M floor — even a huge slope_new doesn't fire,
        protecting against numeric instability from near-zero baselines."""
        # 7d at 100k (sum=700k), 7d at 100k+1d (sum=~701k), 7d at 10M (sum=70M).
        # slope_old = ~701k - 700k = ~1k — way below $1M floor.
        # slope_new is huge, but the floor rejects.
        rows = _seq(
            *([100_000] * 7 + [100_000] * 6 + [101_000] + [10_000_000] * 7),
        )
        assert detector._detect_acceleration("BTC", rows) is None

    def test_zero_slope_old_handled_gracefully(self, detector: AccelerationDetector):
        """Identical oldest and mid window sums → slope_old = 0 → blocked
        by the floor (which uses `<` so 0 < min_prior_usd is True). Must
        not raise ZeroDivisionError on the ratio calc."""
        # 7d at 1M (sum=7M), 7d at 1M (sum=7M), 7d at 5M (sum=35M).
        rows = _seq(*([1_000_000] * 14 + [5_000_000] * 7))
        # Must not raise — the floor catches slope_old=0 before division.
        assert detector._detect_acceleration("BTC", rows) is None

    def test_trigger_data_carries_three_window_decomposition(self, detector: AccelerationDetector):
        """The new keys (`oldest_window_sum_usd`, `mid_window_sum_usd`,
        `slope_old_usd`, `slope_new_usd`, `second_derivative_usd`) are how
        the AI prompt + UI explain WHY this signal fired. Missing keys here
        means a broken explanation downstream."""
        rows = _seq(*([0] * 7 + [1_000_000] * 7 + [3_000_000] * 7))
        hit = detector._detect_acceleration("BTC", rows)
        assert hit is not None
        td = hit.trigger_data
        assert Decimal(td["oldest_window_sum_usd"]) == Decimal("0")
        assert Decimal(td["mid_window_sum_usd"]) == Decimal("7000000")
        assert Decimal(td["recent_window_sum_usd"]) == Decimal("21000000")
        assert Decimal(td["slope_old_usd"]) == Decimal("7000000")
        assert Decimal(td["slope_new_usd"]) == Decimal("14000000")
        assert Decimal(td["second_derivative_usd"]) == Decimal("7000000")
        # `change_ratio` key preserved for FE percent-render compatibility.
        assert "change_ratio" in td
        assert td["window_days"] == 7

    def test_negative_to_positive_slope_flip(self, detector: AccelerationDetector):
        """Trend was getting WORSE (negative slope), now improving (positive
        slope) — fires long. Confirms the direction logic on the
        worst-to-better path."""
        # 7d at 2M (sum=14M), 7d at 1M (sum=7M), 7d at 5M (sum=35M).
        # slope_old = 7M - 14M = -7M
        # slope_new = 35M - 7M = +28M
        # second_derivative = +35M → change_ratio = 35M / -7M = -5.0
        # |5.0| >= 1.0, second_derivative > 0 → direction=long.
        rows = _seq(*([2_000_000] * 7 + [1_000_000] * 7 + [5_000_000] * 7))
        hit = detector._detect_acceleration("BTC", rows)
        assert hit is not None
        assert hit.trigger_data["direction"] == "long"
        assert Decimal(hit.trigger_data["second_derivative_usd"]) > 0


class TestAccelerationFingerprint:
    def test_deterministic(self, detector: AccelerationDetector):
        rows = _seq(*([0] * 7 + [1_000_000] * 7 + [3_000_000] * 7))
        a = detector._detect_acceleration("BTC", rows)
        b = detector._detect_acceleration("BTC", rows)
        assert a is not None and b is not None
        assert a.fingerprint == b.fingerprint

    def test_direction_flips_fingerprint(self, detector: AccelerationDetector):
        long_rows = _seq(*([0] * 7 + [1_000_000] * 7 + [3_000_000] * 7))
        short_rows = _seq(*([0] * 7 + [1_000_000] * 7 + [0] * 7))
        long_hit = detector._detect_acceleration("BTC", long_rows)
        short_hit = detector._detect_acceleration("BTC", short_rows)
        assert long_hit is not None and short_hit is not None
        assert long_hit.fingerprint != short_hit.fingerprint

    def test_threshold_does_not_affect_fingerprint(self):
        """Same guarantee as Magnitude/Divergence — tuning the threshold
        must not change the fingerprint for the same (asset, date,
        direction). Backfills that tweak the threshold can't double-fire."""
        loose = AccelerationDetector(window=7, change_threshold=0.5)
        strict = AccelerationDetector(window=7, change_threshold=2.0)
        rows = _seq(*([0] * 7 + [1_000_000] * 7 + [3_000_000] * 7))
        a = loose._detect_acceleration("BTC", rows)
        b = strict._detect_acceleration("BTC", rows)
        # Loose threshold fires; strict doesn't. So only `a` is non-None;
        # compare a's fingerprint to the canonical expectation.
        assert a is not None
        assert b is None  # 1.0 ratio < 2.0 strict threshold
        # Re-run with a more-permissive strict that still fires, prove
        # same fingerprint regardless.
        strict_2 = AccelerationDetector(window=7, change_threshold=0.9)
        c = strict_2._detect_acceleration("BTC", rows)
        assert c is not None
        assert a.fingerprint == c.fingerprint


class TestAccelerationDBIntegration:
    async def test_detect_against_db(self, db_session):
        """End-to-end — seed 21d of synthetic flows with a strong upward
        acceleration, verify a long hit emerges from the DB-bound `detect()`."""
        from etfpulse.models import ETFFlow

        start = date(2026, 4, 1)
        # Oldest 7d flat at 0, mid 7d at 1M/day, recent 7d at 3M/day.
        synthetic_daily = [Decimal("0")] * 7 + [Decimal("1000000")] * 7 + [Decimal("3000000")] * 7
        for i, flow in enumerate(synthetic_daily):
            db_session.add(
                ETFFlow(
                    asset="BTC",
                    captured_at=start + timedelta(days=i),
                    total_net_flow_usd=flow,
                )
            )
        await db_session.flush()

        hits = await AccelerationDetector().detect(db_session)
        btc = [h for h in hits if h.asset == "BTC"]
        assert len(btc) == 1
        assert btc[0].trigger_data["direction"] == "long"
        assert Decimal(btc[0].trigger_data["second_derivative_usd"]) == Decimal("7000000")
