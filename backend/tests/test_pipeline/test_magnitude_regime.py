"""PR I.4 — `MagnitudeDetector` × regime-conditional threshold integration.

Pure-helper tests live in `test_regime_thresholds.py`. Here we exercise the
detector's `detect()` end-to-end against a real DB session, verifying:

  - Default settings (all mults=1.0) produce the SAME hits as pre-I.4
    (zero behavioural change at defaults).
  - A non-1.0 multiplier actually changes the firing decision.
  - `current_regime=None` is identical to UNCERTAIN — base thresholds.
  - The `trigger_data["percentile"]` field records the EFFECTIVE
    percentile (post-multiplier) so the AI prompt sees what was applied.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from etfpulse.config import settings
from etfpulse.models import ETFFlow, MarketRegime
from etfpulse.pipeline.detectors.magnitude import MagnitudeDetector


async def _seed_history(db_session, asset: str, *, baseline: int, spike: int) -> None:
    """30 flat baseline days + one spike on the latest day. Default
    MagnitudeDetector requires min_history_days=30 to fire."""
    start = date(2026, 3, 1)
    for i in range(29):
        db_session.add(
            ETFFlow(
                asset=asset,
                captured_at=start + timedelta(days=i),
                total_net_flow_usd=Decimal(baseline),
                raw_response={},
            )
        )
    db_session.add(
        ETFFlow(
            asset=asset,
            captured_at=start + timedelta(days=29),
            total_net_flow_usd=Decimal(spike),
            raw_response={},
        )
    )
    await db_session.flush()


class TestDefaultsPreserveBehavior:
    async def test_no_regime_kwarg_matches_explicit_none(self, db_session):
        """Calling `detect(session)` (no kwarg) must be identical to
        `detect(session, current_regime=None)` — same as I.5's `as_of`
        equivalence. Backward compatibility for pre-I.4 callers."""
        await _seed_history(db_session, "BTC", baseline=100_000_000, spike=1_000_000_000)
        await _seed_history(db_session, "ETH", baseline=10_000_000, spike=100_000_000)

        det = MagnitudeDetector()
        no_kwarg = await det.detect(db_session)
        explicit_none = await det.detect(db_session, current_regime=None)
        assert no_kwarg == explicit_none

    async def test_uncertain_regime_matches_none(self, db_session):
        """UNCERTAIN is the explicit "we don't know" regime — must use base
        thresholds, same as None."""
        await _seed_history(db_session, "BTC", baseline=100_000_000, spike=1_000_000_000)
        await _seed_history(db_session, "ETH", baseline=10_000_000, spike=100_000_000)

        det = MagnitudeDetector()
        none_hits = await det.detect(db_session, current_regime=None)
        uncertain_hits = await det.detect(db_session, current_regime=MarketRegime.UNCERTAIN)
        assert none_hits == uncertain_hits

    @pytest.mark.parametrize(
        "regime",
        [
            MarketRegime.MARKUP,
            MarketRegime.MARKDOWN,
            MarketRegime.ACCUMULATION,
            MarketRegime.DISTRIBUTION,
        ],
        ids=lambda r: r.value,
    )
    async def test_default_multipliers_dont_change_hits(self, db_session, regime):
        """All four regime multipliers default to 1.0 → hits MUST be identical
        regardless of which regime is passed. This is the contract that lets
        PR I.4 merge without behavioural risk."""
        await _seed_history(db_session, "BTC", baseline=100_000_000, spike=1_000_000_000)
        await _seed_history(db_session, "ETH", baseline=10_000_000, spike=100_000_000)

        det = MagnitudeDetector()
        base_hits = await det.detect(db_session, current_regime=None)
        regime_hits = await det.detect(db_session, current_regime=regime)
        assert base_hits == regime_hits


class TestNonDefaultMultiplierChangesBehavior:
    async def test_higher_pctile_in_markdown_suppresses_hit(self, db_session, monkeypatch):
        """A high MARKDOWN multiplier raises the effective percentile so a
        moderate outlier no longer qualifies as "top N%". Proves the
        plumbing actually changes the firing decision.

        Seed 28 flat days + 2 equal "spike" days so the spike-value sits at
        the 99th percentile rank — at clamped p=0.99 the latest value
        equals the threshold and the strict `>` check rejects it.
        """
        start = date(2026, 3, 1)
        for asset, scale in (("BTC", 1), ("ETH", 1)):
            for i in range(28):
                db_session.add(
                    ETFFlow(
                        asset=asset,
                        captured_at=start + timedelta(days=i),
                        total_net_flow_usd=Decimal(100_000_000 * scale),
                        raw_response={},
                    )
                )
            # Two equal spikes — at p99, threshold == spike value, strict
            # comparison rejects.
            for i in (28, 29):
                db_session.add(
                    ETFFlow(
                        asset=asset,
                        captured_at=start + timedelta(days=i),
                        total_net_flow_usd=Decimal(200_000_000 * scale),
                        raw_response={},
                    )
                )
        await db_session.flush()

        det = MagnitudeDetector()

        # Base: under default mult=1.0 (effective p=0.80), the spike fires.
        baseline = await det.detect(db_session, current_regime=MarketRegime.MARKDOWN)
        assert len(baseline) > 0

        # Now raise the MARKDOWN multiplier to clamp effective p to 0.99 —
        # the spike no longer strictly exceeds the threshold.
        monkeypatch.setattr(settings, "regime_mult_magnitude_pctile_markdown", Decimal("3.0"))
        suppressed = await det.detect(db_session, current_regime=MarketRegime.MARKDOWN)
        assert len(suppressed) == 0

    async def test_lower_pctile_in_markup_admits_borderline_hit(self, db_session, monkeypatch):
        """Symmetric: a lower MARKUP multiplier loosens the threshold.

        We need varied baselines so a borderline-but-not-extreme value can
        land BETWEEN p50 and p80. Uniform baselines collapse the percentile
        curve to a step function and don't exercise the test scenario.
        """
        # Stepped baseline: flows from 10M to 290M (29 values). Latest
        # day's value (220M) lands BETWEEN p50 (≈150M) and p80 (≈230M).
        start = date(2026, 3, 1)
        for asset, scale in (("BTC", 1), ("ETH", 1)):
            for i in range(29):
                db_session.add(
                    ETFFlow(
                        asset=asset,
                        captured_at=start + timedelta(days=i),
                        total_net_flow_usd=Decimal((i + 1) * 10_000_000 * scale),
                        raw_response={},
                    )
                )
            db_session.add(
                ETFFlow(
                    asset=asset,
                    captured_at=start + timedelta(days=29),
                    total_net_flow_usd=Decimal(220_000_000 * scale),
                    raw_response={},
                )
            )
        await db_session.flush()

        det = MagnitudeDetector()
        # Base p80: 220M doesn't clear → no hit.
        base = await det.detect(db_session, current_regime=MarketRegime.MARKUP)
        assert base == []

        # Lower the percentile via MARKUP mult — p50 admits 220M.
        monkeypatch.setattr(
            settings, "regime_mult_magnitude_pctile_markup", Decimal("0.625")
        )  # 0.80 × 0.625 = 0.50
        loosened = await det.detect(db_session, current_regime=MarketRegime.MARKUP)
        assert len(loosened) > 0


class TestTriggerDataRecordsEffectivePercentile:
    async def test_trigger_data_percentile_is_post_multiplier(self, db_session, monkeypatch):
        """`trigger_data["percentile"]` is what the detector ACTUALLY used —
        post-multiplier. An operator reading the signal's trigger_data must
        see the effective threshold, not the base, so the AI prompt + UI
        reflect the decision the detector made."""
        await _seed_history(db_session, "BTC", baseline=100_000_000, spike=1_000_000_000)
        await _seed_history(db_session, "ETH", baseline=10_000_000, spike=100_000_000)

        monkeypatch.setattr(settings, "regime_mult_magnitude_pctile_markup", Decimal("0.9"))
        det = MagnitudeDetector()
        hits = await det.detect(db_session, current_regime=MarketRegime.MARKUP)
        assert len(hits) > 0
        # Default base is 0.80; 0.80 × 0.9 = 0.72.
        for hit in hits:
            assert abs(hit.trigger_data["percentile"] - 0.72) < 1e-9
