"""PR I.3b — pure-function tests for the composite-scoring module.

No DB, no clock — every test is a truth-table pin. Covers:
  * `weighted_composite_return` math (equal weights, skewed weights, edges).
  * `classify_composite_outcome` decision boundaries (long/short, hit/miss,
    `direction == 0` short-circuit).

The settings-level invariant (weights sum to 1.0) is pinned in the config
tests; this module trusts that contract and only asserts the math.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from etfpulse.pipeline.composite_scoring import (
    classify_composite_outcome,
    weighted_composite_return,
)


class TestWeightedCompositeReturn:
    def test_equal_weights_average(self):
        # 0.5 * 0.04 + 0.5 * 0.02 = 0.03
        result = weighted_composite_return(
            btc_return_pct=Decimal("0.04"),
            eth_return_pct=Decimal("0.02"),
            btc_weight=Decimal("0.5"),
            eth_weight=Decimal("0.5"),
        )
        assert result == Decimal("0.03")

    def test_skewed_weights_dominate(self):
        # 0.9 * 0.05 + 0.1 * (-0.05) = 0.045 - 0.005 = 0.04
        result = weighted_composite_return(
            btc_return_pct=Decimal("0.05"),
            eth_return_pct=Decimal("-0.05"),
            btc_weight=Decimal("0.9"),
            eth_weight=Decimal("0.1"),
        )
        assert result == Decimal("0.04")

    def test_all_btc_weight_returns_btc_only(self):
        result = weighted_composite_return(
            btc_return_pct=Decimal("0.07"),
            eth_return_pct=Decimal("-0.99"),
            btc_weight=Decimal("1.0"),
            eth_weight=Decimal("0.0"),
        )
        assert result == Decimal("0.07")

    def test_both_negative_returns_negative(self):
        result = weighted_composite_return(
            btc_return_pct=Decimal("-0.02"),
            eth_return_pct=Decimal("-0.04"),
            btc_weight=Decimal("0.5"),
            eth_weight=Decimal("0.5"),
        )
        assert result == Decimal("-0.03")

    def test_zero_returns_yields_zero(self):
        result = weighted_composite_return(
            btc_return_pct=Decimal("0"),
            eth_return_pct=Decimal("0"),
            btc_weight=Decimal("0.5"),
            eth_weight=Decimal("0.5"),
        )
        assert result == Decimal("0")


class TestClassifyCompositeOutcome:
    @pytest.mark.parametrize(
        ("composite", "direction", "hit_pct", "expected"),
        [
            # Long: composite × direction = composite (since direction=+1).
            # Hit when composite >= hit_pct.
            (Decimal("0.03"), 1, Decimal("0.02"), True),  # +3% long, 2% threshold → hit
            (Decimal("0.02"), 1, Decimal("0.02"), True),  # exact threshold → hit
            (Decimal("0.019"), 1, Decimal("0.02"), False),  # just below → miss
            (Decimal("-0.05"), 1, Decimal("0.02"), False),  # wrong direction → miss
            # Short: signed_progress = composite × -1.
            (Decimal("-0.03"), -1, Decimal("0.02"), True),  # -3% short, 2% threshold → hit
            (Decimal("-0.02"), -1, Decimal("0.02"), True),  # exact → hit
            (Decimal("-0.019"), -1, Decimal("0.02"), False),  # just below → miss
            (Decimal("0.05"), -1, Decimal("0.02"), False),  # wrong direction → miss
            # Edge: zero composite is never a hit (threshold is strictly positive).
            (Decimal("0"), 1, Decimal("0.02"), False),
            (Decimal("0"), -1, Decimal("0.02"), False),
        ],
    )
    def test_truth_table(self, composite, direction, hit_pct, expected):
        result = classify_composite_outcome(
            composite_return_pct=composite,
            direction=direction,
            hit_pct=hit_pct,
        )
        assert result is expected

    def test_direction_zero_returns_none(self):
        # `wait` / unknown action → caller MUST skip writing an outcome row.
        # The single contract for "unscorable" is `None`, not False.
        result = classify_composite_outcome(
            composite_return_pct=Decimal("0.05"),
            direction=0,
            hit_pct=Decimal("0.02"),
        )
        assert result is None
