"""Look-ahead defense — every detector MUST ignore source rows dated strictly
after `as_of` (PR I.5, rule D25). This is the backtest seam: a date-clamped
call must return the same hits as if the future row never existed.

Pure unit tests on pre-buckets aren't sufficient because the look-ahead leak
lives in the SQL layer (the `select(...).where(...)` query). These tests
exercise the real DB path: seed past + future rows, call `detect(session,
as_of=T)`, assert the result equals the no-future-row baseline.

One file covers all 5 detectors because the invariant is the same — equality
of (`detect()` over [past only]) vs (`detect(as_of=T)` over [past + future]).
That equality is the look-ahead defense. We do NOT pin specific hit shapes
here — the per-detector behaviour suites do that. Here we pin the seam.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from etfpulse.models import ETFFlow, MarketRegime, RegimeSnapshot, SignalPosture
from etfpulse.pipeline.detectors.acceleration import AccelerationDetector
from etfpulse.pipeline.detectors.divergence import DivergenceDetector
from etfpulse.pipeline.detectors.flow_anomaly import FlowAnomalyDetector
from etfpulse.pipeline.detectors.magnitude import MagnitudeDetector
from etfpulse.pipeline.detectors.regime_shift import RegimeShiftDetector


def _flow(asset: str, d: date, usd: int) -> ETFFlow:
    return ETFFlow(
        asset=asset,
        captured_at=d,
        total_net_flow_usd=Decimal(str(usd)),
        raw_response={},
    )


async def _seed_flows(session, asset: str, *, start: date, flows: list[int]) -> None:
    for i, f in enumerate(flows):
        session.add(_flow(asset, start + timedelta(days=i), f))
    await session.flush()


class TestFlowAnomalyLookAhead:
    async def test_future_row_does_not_alter_past_detection(self, db_session):
        det = FlowAnomalyDetector()
        # Past: 3-day long streak then a break on day 4 — fires a hit.
        await _seed_flows(db_session, "BTC", start=date(2026, 4, 1), flows=[100, 200, 300, -50])
        await _seed_flows(db_session, "ETH", start=date(2026, 4, 1), flows=[10, 20, 30, -5])
        baseline = sorted(
            await det.detect(db_session, as_of=date(2026, 4, 4)),
            key=lambda h: h.asset,
        )

        # Future: a big inflow that would extend the streak narrative and
        # change "latest break" detection if seen.
        db_session.add(_flow("BTC", date(2026, 4, 5), 9_000_000))
        db_session.add(_flow("ETH", date(2026, 4, 5), 900_000))
        await db_session.flush()

        clamped = sorted(
            await det.detect(db_session, as_of=date(2026, 4, 4)),
            key=lambda h: h.asset,
        )
        assert clamped == baseline
        # Sanity: without as_of, results would differ.
        unclamped = sorted(await det.detect(db_session), key=lambda h: h.asset)
        assert unclamped != baseline


class TestMagnitudeLookAhead:
    async def test_future_row_does_not_alter_past_detection(self, db_session):
        # Tight thresholds so the test scenario fires deterministically on 30 rows.
        det = MagnitudeDetector(lookback_days=90, percentile_threshold=0.80, min_history_days=30)
        # 30 baseline days; latest day is the magnitude outlier.
        baseline_flows = [100_000_000] * 29 + [1_000_000_000]
        await _seed_flows(db_session, "BTC", start=date(2026, 3, 1), flows=baseline_flows)
        await _seed_flows(db_session, "ETH", start=date(2026, 3, 1), flows=baseline_flows)
        as_of = date(2026, 3, 1) + timedelta(days=29)
        baseline = sorted(await det.detect(db_session, as_of=as_of), key=lambda h: h.asset)

        # Future: another outlier that would shift the percentile + change "latest day".
        db_session.add(_flow("BTC", as_of + timedelta(days=1), 5_000_000_000))
        db_session.add(_flow("ETH", as_of + timedelta(days=1), 5_000_000_000))
        await db_session.flush()
        clamped = sorted(await det.detect(db_session, as_of=as_of), key=lambda h: h.asset)
        assert clamped == baseline


class TestAccelerationLookAhead:
    async def test_future_row_does_not_alter_past_detection(self, db_session):
        det = AccelerationDetector(
            window=7,
            change_threshold=1.00,
            min_slope_old_usd=Decimal("1000000"),
        )
        # 21-day window: oldest flat, mid rising, recent surging → fires long.
        flows = [10_000_000] * 7 + [50_000_000] * 7 + [200_000_000] * 7
        await _seed_flows(db_session, "BTC", start=date(2026, 4, 1), flows=flows)
        await _seed_flows(db_session, "ETH", start=date(2026, 4, 1), flows=flows)
        as_of = date(2026, 4, 1) + timedelta(days=20)
        baseline = sorted(await det.detect(db_session, as_of=as_of), key=lambda h: h.asset)

        # Future row collapses recent window → would flip direction or kill the hit.
        for d in range(7):
            day = as_of + timedelta(days=1 + d)
            db_session.add(_flow("BTC", day, -500_000_000))
            db_session.add(_flow("ETH", day, -500_000_000))
        await db_session.flush()
        clamped = sorted(await det.detect(db_session, as_of=as_of), key=lambda h: h.asset)
        assert clamped == baseline


class TestDivergenceLookAhead:
    """Divergence reads ETFFlow AND klines; its kline lookup is derived from
    flow-row dates (which `as_of` already clamps) and `_closest_close_on_or_before`
    walks backward only. So clamping flows alone is sufficient — no separate
    kline gate is needed. We don't pin whether divergence fires (depends on
    the kline fixture for those dates); we pin EQUALITY between clamped + baseline.
    """

    async def test_future_row_does_not_alter_past_detection(self, db_session):
        det = DivergenceDetector()
        await _seed_flows(
            db_session, "BTC", start=date(2026, 4, 1), flows=[100_000_000, 200_000_000, 300_000_000]
        )
        await _seed_flows(
            db_session, "ETH", start=date(2026, 4, 1), flows=[50_000_000, 60_000_000, 70_000_000]
        )
        as_of = date(2026, 4, 3)
        baseline = sorted(
            await det.detect(db_session, as_of=as_of), key=lambda h: (h.asset, h.signal_date)
        )

        # Future: a 4th flow row that would shift the latest window.
        db_session.add(_flow("BTC", date(2026, 4, 4), -800_000_000))
        db_session.add(_flow("ETH", date(2026, 4, 4), -400_000_000))
        await db_session.flush()
        clamped = sorted(
            await det.detect(db_session, as_of=as_of), key=lambda h: (h.asset, h.signal_date)
        )
        assert clamped == baseline


class TestRegimeShiftLookAhead:
    async def test_future_snapshot_does_not_alter_past_detection(self, db_session):
        det = RegimeShiftDetector()
        d1 = datetime(2026, 4, 10, 12, 0, tzinfo=UTC)
        d2 = datetime(2026, 4, 11, 12, 0, tzinfo=UTC)
        db_session.add(
            RegimeSnapshot(
                captured_at=d1,
                regime=MarketRegime.ACCUMULATION.value,
                signal_posture=SignalPosture.NORMAL.value,
                confidence=7,
            )
        )
        db_session.add(
            RegimeSnapshot(
                captured_at=d2,
                regime=MarketRegime.MARKUP.value,
                signal_posture=SignalPosture.NORMAL.value,
                confidence=7,
            )
        )
        await db_session.flush()
        as_of = date(2026, 4, 11)
        baseline = await det.detect(db_session, as_of=as_of)
        assert len(baseline) == 1  # ACCUMULATION → MARKUP fired

        # Future snapshot that would become the "latest" if seen — flipping
        # the comparison to MARKUP → MARKDOWN and changing the hit's
        # fingerprint (different new_regime).
        db_session.add(
            RegimeSnapshot(
                captured_at=datetime(2026, 4, 12, 12, 0, tzinfo=UTC),
                regime=MarketRegime.MARKDOWN.value,
                signal_posture=SignalPosture.NORMAL.value,
                confidence=7,
            )
        )
        await db_session.flush()
        clamped = await det.detect(db_session, as_of=as_of)
        assert clamped == baseline


@pytest.mark.parametrize(
    "detector",
    [
        FlowAnomalyDetector(),
        MagnitudeDetector(),
        AccelerationDetector(),
        DivergenceDetector(),
        RegimeShiftDetector(),
    ],
    ids=lambda d: d.name,
)
async def test_as_of_none_preserves_pre_i5_behaviour(detector, db_session):
    """`as_of=None` (the default, the production call shape) MUST be exactly
    equivalent to calling `detect(session)` with no kwarg at all. If a detector
    sneaks in a behavioural difference between the two, production callers
    (`signal_builder.run_daily_cycle`) would silently drift."""
    explicit_none = await detector.detect(db_session, as_of=None)
    no_kwarg = await detector.detect(db_session)
    assert explicit_none == no_kwarg
