"""Pure-helper tests for `pipeline.regime_thresholds` (PR I.4).

The helper is the single regime → multiplier mapping for `MagnitudeDetector`'s
percentile threshold. All tests here are pure — no DB, no fixtures. Anchors
the defaults-preserve-behaviour invariant and the clamp semantic so a future
PR can't silently break either.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from etfpulse.config import settings
from etfpulse.models import MarketRegime
from etfpulse.pipeline.regime_thresholds import (
    _PCT_MAX,
    _PCT_MIN,
    apply_magnitude_pctile_multiplier,
)


class TestDefaultsArePassThrough:
    """With all multipliers at 1.0 (the shipped default), every regime
    returns base unchanged + was_clamped=False. This is the contract that
    makes PR I.4 risk-free to merge: defaults are bit-for-bit identical to
    pre-I.4 magnitude behaviour."""

    @pytest.mark.parametrize(
        "regime",
        [
            MarketRegime.MARKUP,
            MarketRegime.MARKDOWN,
            MarketRegime.ACCUMULATION,
            MarketRegime.DISTRIBUTION,
            MarketRegime.UNCERTAIN,
            None,
        ],
        ids=lambda r: r.value if r is not None else "none",
    )
    def test_default_settings_pass_through(self, regime):
        result, clamped = apply_magnitude_pctile_multiplier(0.80, regime)
        assert result == 0.80
        assert clamped is False


class TestRegimeMultipliers:
    def test_markdown_multiplier_scales_threshold(self, monkeypatch):
        monkeypatch.setattr(settings, "regime_mult_magnitude_pctile_markdown", Decimal("1.1"))
        result, clamped = apply_magnitude_pctile_multiplier(0.80, MarketRegime.MARKDOWN)
        # 0.80 × 1.1 = 0.88 — inside the (0.01, 0.99) clamp range.
        assert abs(result - 0.88) < 1e-9
        assert clamped is False

    def test_markup_multiplier_can_lower_threshold(self, monkeypatch):
        monkeypatch.setattr(settings, "regime_mult_magnitude_pctile_markup", Decimal("0.5"))
        result, clamped = apply_magnitude_pctile_multiplier(0.80, MarketRegime.MARKUP)
        assert abs(result - 0.40) < 1e-9
        assert clamped is False

    def test_uncertain_is_always_passthrough_regardless_of_other_settings(self, monkeypatch):
        """No env var maps to UNCERTAIN — applying a multiplier to "we don't
        know" would overcommit. Even if other regime mults are tuned,
        UNCERTAIN stays at 1.0."""
        monkeypatch.setattr(settings, "regime_mult_magnitude_pctile_markdown", Decimal("1.5"))
        monkeypatch.setattr(settings, "regime_mult_magnitude_pctile_markup", Decimal("0.5"))
        result, clamped = apply_magnitude_pctile_multiplier(0.80, MarketRegime.UNCERTAIN)
        assert result == 0.80
        assert clamped is False

    def test_none_regime_is_passthrough(self):
        """`current_regime=None` is the orchestrator's "no snapshot available"
        signal. Detectors must use base thresholds — same as UNCERTAIN."""
        result, clamped = apply_magnitude_pctile_multiplier(0.80, None)
        assert result == 0.80
        assert clamped is False


class TestForwardCompatibility:
    """The `_magnitude_pctile_multiplier_for` docstring promises that adding
    a regime to `MarketRegime` without extending the match-statement falls
    through to the default-1.0 branch (fail-safe rather than crash). This
    pins that promise.

    We can't synthesise a real `MarketRegime` enum value the helper doesn't
    know about (StrEnum equality would still match an existing case if the
    value collides), so we pass a string that doesn't equal any of the 5
    enum values. The match-statement's class-value patterns reduce to
    equality checks; an unmatched string falls through to `case _:`.
    """

    def test_unknown_regime_falls_through_to_default(self):
        from etfpulse.pipeline.regime_thresholds import _magnitude_pctile_multiplier_for

        # Pass a string with no matching enum — exercises the defensive
        # `case _:` branch documented in the helper's docstring.
        mult = _magnitude_pctile_multiplier_for("future_regime_value")  # type: ignore[arg-type]
        assert mult == Decimal("1.0")

    def test_apply_helper_handles_unknown_regime_safely(self):
        # End-to-end: the public helper must also fall safe.
        result, clamped = apply_magnitude_pctile_multiplier(0.80, "future_regime_value")  # type: ignore[arg-type]
        assert result == 0.80
        assert clamped is False


class TestClamp:
    """A mult that produces effective threshold outside (0.01, 0.99) is
    clamped to that range. The clamp flag is surfaced for caller logging."""

    def test_clamps_above_max(self, monkeypatch):
        # 0.80 × 1.5 = 1.20 — out of valid range.
        monkeypatch.setattr(settings, "regime_mult_magnitude_pctile_markdown", Decimal("1.5"))
        result, clamped = apply_magnitude_pctile_multiplier(0.80, MarketRegime.MARKDOWN)
        assert result == float(_PCT_MAX)
        assert clamped is True

    def test_clamps_below_min(self, monkeypatch):
        # 0.05 × 0.1 = 0.005 — below min (0.01).
        monkeypatch.setattr(settings, "regime_mult_magnitude_pctile_markup", Decimal("0.1"))
        result, clamped = apply_magnitude_pctile_multiplier(0.05, MarketRegime.MARKUP)
        assert result == float(_PCT_MIN)
        assert clamped is True

    def test_in_range_not_clamped(self, monkeypatch):
        monkeypatch.setattr(settings, "regime_mult_magnitude_pctile_markdown", Decimal("1.2"))
        result, clamped = apply_magnitude_pctile_multiplier(0.5, MarketRegime.MARKDOWN)
        # 0.5 × 1.2 = 0.60 — inside range.
        assert abs(result - 0.60) < 1e-9
        assert clamped is False
