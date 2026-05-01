"""RegimeShiftDetector — DB-driven; no pure helpers worth pinning separately.

These tests seed `regime_snapshots` directly (rather than running the
classifier) so failures pin behavior to the detector's compare logic, not the
classifier's score thresholds.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

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

    async def test_transition_emits_two_hits_one_per_asset(self, db_session, detector):
        """One row per tracked asset (BTC + ETH) — symmetry with other detectors,
        no MARKET sentinel polluting fan-out."""
        now = datetime.now(UTC)
        db_session.add(
            _snapshot(captured_at=now - timedelta(days=1), regime=MarketRegime.ACCUMULATION)
        )
        db_session.add(_snapshot(captured_at=now, regime=MarketRegime.MARKUP))
        await db_session.flush()

        hits = await detector.detect(db_session)

        assets = sorted(h.asset for h in hits)
        assert assets == ["BTC", "ETH"]
        for hit in hits:
            assert hit.signal_type == "regime_shift"
            assert hit.trigger_data["previous_regime"] == "accumulation"
            assert hit.trigger_data["new_regime"] == "markup"
            assert hit.signal_date == now.astimezone(UTC).date()
            assert len(hit.fingerprint) == 32

    async def test_per_asset_fingerprints_differ(self, db_session, detector):
        """Same content, different asset → fingerprints must differ so the
        unique index doesn't reject the second insert."""
        now = datetime.now(UTC)
        db_session.add(_snapshot(captured_at=now - timedelta(days=1), regime=MarketRegime.MARKDOWN))
        db_session.add(_snapshot(captured_at=now, regime=MarketRegime.UNCERTAIN))
        await db_session.flush()

        hits = await detector.detect(db_session)
        fingerprints = {h.fingerprint for h in hits}
        assert len(fingerprints) == len(hits)

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
