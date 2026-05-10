"""Detector framework — `compute_fingerprint` invariants and registry shape.

Fingerprint determinism is load-bearing: if it ever changes for the same
inputs, every existing signal becomes orphaned and detectors start
double-firing. These tests pin the exact behaviour.
"""

from __future__ import annotations

from datetime import date

import pytest

from etfpulse.pipeline.detectors import ALL_DETECTORS, Detector, DetectorHit, compute_fingerprint


class TestComputeFingerprint:
    def test_is_deterministic(self):
        assert compute_fingerprint("btc", "flow_anomaly", "long") == compute_fingerprint(
            "btc", "flow_anomaly", "long"
        )

    def test_length_is_32_hex(self):
        fp = compute_fingerprint("anything")
        assert len(fp) == 32
        assert all(c in "0123456789abcdef" for c in fp)

    def test_different_inputs_produce_different_fingerprints(self):
        assert compute_fingerprint("btc", "long") != compute_fingerprint("eth", "long")
        assert compute_fingerprint("btc", "long") != compute_fingerprint("btc", "short")

    def test_nul_join_disambiguates_concatenation(self):
        # The NUL separator is the only thing preventing these from colliding.
        # If anyone "optimises" by removing the join character, this test fires.
        assert compute_fingerprint("btc", "long") != compute_fingerprint("btcl", "ong")
        assert compute_fingerprint("a", "bc") != compute_fingerprint("ab", "c")

    def test_whitespace_is_stripped(self):
        assert compute_fingerprint("btc", "long") == compute_fingerprint("  btc  ", "\tlong\n")

    def test_order_matters(self):
        assert compute_fingerprint("btc", "long") != compute_fingerprint("long", "btc")

    def test_no_parts_raises(self):
        with pytest.raises(ValueError, match="at least one part"):
            compute_fingerprint()

    def test_empty_string_raises(self):
        with pytest.raises(ValueError, match="empty after strip"):
            compute_fingerprint("btc", "")

    def test_whitespace_only_raises(self):
        with pytest.raises(ValueError, match="empty after strip"):
            compute_fingerprint("btc", "   ")

    def test_non_string_raises(self):
        # Floats are the dangerous case — banning them at the type guard is
        # the whole point.
        with pytest.raises(TypeError, match="must be str, got float"):
            compute_fingerprint("btc", 2.0)  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="must be str, got int"):
            compute_fingerprint("btc", 2)  # type: ignore[arg-type]


class TestDetectorHit:
    def test_construct(self):
        hit = DetectorHit(
            signal_type="flow_anomaly",
            asset="btc",
            signal_date=date(2026, 4, 23),
            trigger_data={"zscore_bucket": "2sigma"},
            fingerprint=compute_fingerprint("flow_anomaly", "btc", "2sigma_long"),
        )
        assert hit.asset == "btc"
        assert len(hit.fingerprint) == 32

    def test_is_frozen(self):
        hit = DetectorHit(
            signal_type="flow_anomaly",
            asset="btc",
            signal_date=date(2026, 4, 23),
            trigger_data={},
            fingerprint="x" * 32,
        )
        with pytest.raises(AttributeError):
            hit.asset = "eth"  # type: ignore[misc]


class TestRegistry:
    def test_contains_all_registered_detectors(self):
        # If you add a detector to `pipeline/detectors/` and forget the
        # `ALL_DETECTORS.append(...)` line in `__init__.py`, this test fires.
        # Stage 4 added flow_anomaly/magnitude/acceleration; Stage 7 added
        # divergence + regime_shift.
        names = {d.name for d in ALL_DETECTORS}
        assert {
            "flow_anomaly",
            "magnitude",
            "acceleration",
            "divergence",
            "regime_shift",
        }.issubset(names)

    def test_names_are_unique(self):
        # Two detectors with the same name would make logs ambiguous and
        # break per-detector metrics.
        names = [d.name for d in ALL_DETECTORS]
        assert len(names) == len(set(names))

    def test_registry_threshold_values_match_settings(self):
        """Issue #33 — ALL_DETECTORS must construct from settings, not hardcoded
        defaults. Test confirms each tunable detector picked up the value
        from `etfpulse.config.settings`. RegimeShift has no tunables, skipped."""
        from etfpulse.config import settings

        by_name = {d.name: d for d in ALL_DETECTORS}

        flow = by_name["flow_anomaly"]
        assert flow.lookback_days == settings.flow_anomaly_lookback_days
        assert flow.min_streak_length == settings.flow_anomaly_min_streak_length

        mag = by_name["magnitude"]
        assert mag.lookback_days == settings.magnitude_lookback_days
        assert mag.percentile_threshold == settings.magnitude_percentile_threshold
        assert mag.min_history_days == settings.magnitude_min_history_days

        acc = by_name["acceleration"]
        assert acc.window == settings.acceleration_window
        assert acc.change_threshold == settings.acceleration_change_threshold
        assert acc.min_prior_usd == settings.acceleration_min_prior_usd

        div = by_name["divergence"]
        assert div.lookback_days == settings.divergence_lookback_days

    def test_protocol_is_structurally_satisfiable(self):
        # Smoke-test that a minimal detector matches the Protocol shape —
        # this is what `signal_builder` will rely on.
        class _StubDetector:
            name = "stub"
            signal_type = "flow_anomaly"

            async def detect(self, session):  # type: ignore[no-untyped-def]
                return []

        detector: Detector = _StubDetector()
        assert detector.name == "stub"
