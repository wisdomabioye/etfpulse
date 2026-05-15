"""RegimeShiftDetector — DB-driven; no pure helpers worth pinning separately.

These tests seed `regime_snapshots` directly (rather than running the
classifier) so failures pin behavior to the detector's compare logic, not the
classifier's score thresholds.

PR F.3 — restructured for the new contract:
  * Single MARKET hit per cross-UTC-day transition.
  * Same-UTC-day flicker is gated out (issue #46 closure).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from etfpulse.constants import MARKET_ASSET
from etfpulse.models import MarketRegime, RegimeSnapshot, SignalPosture
from etfpulse.pipeline.detectors.regime_shift import RegimeShiftDetector


def _snapshot(
    *,
    captured_at: datetime,
    regime: MarketRegime,
    posture: SignalPosture = SignalPosture.NORMAL,
    confidence: int = 7,
) -> RegimeSnapshot:
    return RegimeSnapshot(
        captured_at=captured_at,
        regime=regime.value,
        signal_posture=posture.value,
        confidence=confidence,
    )


@pytest.fixture
def detector() -> RegimeShiftDetector:
    return RegimeShiftDetector()


class TestDetect:
    async def test_no_snapshots_returns_empty(self, db_session, detector):
        assert await detector.detect(db_session) == []

    async def test_single_snapshot_returns_empty(self, db_session, detector):
        """Need TWO snapshots to detect a transition; one isn't enough."""
        db_session.add(_snapshot(captured_at=datetime.now(UTC), regime=MarketRegime.MARKUP))
        await db_session.flush()
        assert await detector.detect(db_session) == []

    async def test_no_transition_returns_empty(self, db_session, detector):
        """Same regime in both snapshots → no shift to detect."""
        now = datetime.now(UTC)
        db_session.add(_snapshot(captured_at=now - timedelta(days=1), regime=MarketRegime.MARKUP))
        db_session.add(_snapshot(captured_at=now, regime=MarketRegime.MARKUP))
        await db_session.flush()
        assert await detector.detect(db_session) == []

    async def test_cross_day_transition_emits_single_market_hit(self, db_session, detector):
        """PR F.3 — one MARKET hit per transition, not one-per-asset.

        Previously emitted two hits (BTC + ETH) with identical content; the
        downstream cohort treated them as separate signals which inflated
        the regime_shift count and diluted track-record stats. New contract:
        single hit, asset=MARKET, fan-out bypasses pref_assets.
        """
        now = datetime.now(UTC)
        db_session.add(
            _snapshot(captured_at=now - timedelta(days=1), regime=MarketRegime.ACCUMULATION)
        )
        db_session.add(_snapshot(captured_at=now, regime=MarketRegime.MARKUP))
        await db_session.flush()

        hits = await detector.detect(db_session)

        assert len(hits) == 1
        hit = hits[0]
        assert hit.asset == MARKET_ASSET
        assert hit.signal_type == "regime_shift"
        assert hit.trigger_data["previous_regime"] == "accumulation"
        assert hit.trigger_data["new_regime"] == "markup"
        assert hit.signal_date == now.astimezone(UTC).date()
        assert len(hit.fingerprint) == 32
        # Fingerprint is asset-pinned to MARKET — encodes the new contract
        # so a future regression that loops over assets would emit a
        # different fingerprint and be caught at the unique-index level.
        from etfpulse.pipeline.detectors.base import compute_fingerprint

        assert hit.fingerprint == compute_fingerprint(
            MARKET_ASSET, "regime_shift", hit.signal_date.isoformat(), "markup"
        )

    async def test_same_utc_day_transition_is_gated(self, db_session, detector):
        """Issue #46 closure — intra-day re-classification that flips regimes
        within a single UTC day must NOT fire a signal.

        Pre-Branch-3 (no intra-day cycle): regimes only got classified once
        per day at the daily cron, so adjacent snapshots always straddled
        midnight. After Branch 3 added the hourly intra-day cycle, regimes
        could oscillate within a day, producing noise signals.

        Gate: `previous.captured_at.date() < latest.captured_at.date()`.
        Same-day → no hit.
        """
        # Two snapshots on the SAME UTC date, different regimes.
        midday = datetime(2026, 5, 15, 12, 0, 0, tzinfo=UTC)
        later_same_day = datetime(2026, 5, 15, 14, 30, 0, tzinfo=UTC)
        db_session.add(_snapshot(captured_at=midday, regime=MarketRegime.ACCUMULATION))
        db_session.add(_snapshot(captured_at=later_same_day, regime=MarketRegime.MARKUP))
        await db_session.flush()

        assert await detector.detect(db_session) == []

    async def test_midnight_boundary_fires(self, db_session, detector):
        """Boundary case — previous at 23:59 UTC day N, latest at 00:01 UTC
        day N+1. The gate is `<` (strict), so adjacent-minute snapshots
        across midnight still count as different days and fire."""
        late_yesterday = datetime(2026, 5, 14, 23, 59, 0, tzinfo=UTC)
        early_today = datetime(2026, 5, 15, 0, 1, 0, tzinfo=UTC)
        db_session.add(_snapshot(captured_at=late_yesterday, regime=MarketRegime.ACCUMULATION))
        db_session.add(_snapshot(captured_at=early_today, regime=MarketRegime.MARKUP))
        await db_session.flush()

        hits = await detector.detect(db_session)

        assert len(hits) == 1
        assert hits[0].signal_date == early_today.date()
        assert hits[0].trigger_data["new_regime"] == "markup"

    async def test_re_run_is_idempotent(self, db_session, detector):
        """Calling detect twice over the same DB state produces equivalent
        hits. The (fingerprint, signal_date) unique index on `signals` makes
        a downstream re-insert a no-op, so the detector itself is allowed to
        return the same hit set every time. This pins that property under
        the new single-MARKET-hit contract.
        """
        now = datetime.now(UTC)
        db_session.add(_snapshot(captured_at=now - timedelta(days=1), regime=MarketRegime.MARKDOWN))
        db_session.add(_snapshot(captured_at=now, regime=MarketRegime.ACCUMULATION))
        await db_session.flush()

        first = await detector.detect(db_session)
        second = await detector.detect(db_session)

        assert len(first) == 1
        assert len(second) == 1
        assert first[0].fingerprint == second[0].fingerprint
        assert first[0].asset == second[0].asset == MARKET_ASSET

    async def test_distinct_target_regimes_produce_distinct_fingerprints(
        self, db_session, detector
    ):
        """Two different transitions on different days (accumulation→markup
        vs markup→markdown) must produce different fingerprints so the
        unique index admits both.

        Replaces the pre-F.3 `test_per_asset_fingerprints_differ` which
        pinned BTC vs ETH fingerprints — irrelevant under single-MARKET-hit.
        """
        day0 = datetime(2026, 5, 13, 6, 0, 0, tzinfo=UTC)
        day1 = datetime(2026, 5, 14, 6, 0, 0, tzinfo=UTC)
        day2 = datetime(2026, 5, 15, 6, 0, 0, tzinfo=UTC)
        db_session.add(_snapshot(captured_at=day0, regime=MarketRegime.ACCUMULATION))
        db_session.add(_snapshot(captured_at=day1, regime=MarketRegime.MARKUP))
        await db_session.flush()
        hit_a = (await detector.detect(db_session))[0]

        # Add the next day's transition (markup → markdown).
        db_session.add(_snapshot(captured_at=day2, regime=MarketRegime.MARKDOWN))
        await db_session.flush()
        hit_b = (await detector.detect(db_session))[0]

        assert hit_a.fingerprint != hit_b.fingerprint

    async def test_legacy_null_regime_is_skipped(self, db_session, detector):
        """A pre-classifier snapshot (regime IS NULL) cannot anchor a comparison
        — must not crash, must return no hits."""
        now = datetime.now(UTC)
        # Older row with no regime column populated (pre-Stage-7 shape).
        db_session.add(RegimeSnapshot(captured_at=now - timedelta(days=2)))
        # Today's row populated normally.
        db_session.add(_snapshot(captured_at=now, regime=MarketRegime.MARKUP))
        await db_session.flush()

        assert await detector.detect(db_session) == []
