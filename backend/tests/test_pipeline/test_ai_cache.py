"""Tests for `pipeline.ai_cache` — file-based AI response cache (PR I.5/I.4).

We isolate every test from on-disk state by monkey-patching `CACHE_ROOT` to
a per-test `tmp_path`. The real cache root (`backend/.backtest_cache/`) is
never touched by tests.

PR I.4 added the `trigger_hash` component to the cache key so any drift in
`DetectorHit.trigger_data` (regime-driven percentile shifts, future per-
detector knobs) invalidates the cache automatically.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from etfpulse.pipeline import ai_cache
from etfpulse.pipeline.analysis import AISignalAnalysis

# Stable hash literals used across tests — values don't matter, only that
# they're consistent within a test. The real hash comes from
# `ai_cache.hash_trigger_data` which is exercised in its own class below.
_H1 = "abcdef0123456789"
_H2 = "fedcba9876543210"


def _make_analysis(**overrides: object) -> AISignalAnalysis:
    base: dict[str, object] = {
        "headline": "ETF flow spike",
        "reasoning": ["sustained inflow"],
        "confidence": 7,
        "risks": ["FOMC nearby"],
        "suggested_action": "consider long",
        "time_horizon": "swing",
        "entry_price": Decimal("84200"),
        "stop_price": Decimal("82000"),
        "target_price": Decimal("89500"),
    }
    base.update(overrides)
    return AISignalAnalysis.model_validate(base)


@pytest.fixture(autouse=True)
def _redirect_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(ai_cache, "CACHE_ROOT", tmp_path)
    return tmp_path


class TestHashTriggerData:
    """The `trigger_hash` derivation must be stable (same input → same hash)
    and order-independent (dict key order can't drift the hash). It also
    must produce DIFFERENT hashes for different inputs — that's the whole
    point of including it in the cache key under PR I.4."""

    def test_same_dict_produces_same_hash(self):
        td = {"signal_date": "2026-04-06", "direction": "long", "percentile": 0.80}
        assert ai_cache.hash_trigger_data(td) == ai_cache.hash_trigger_data(td)

    def test_key_order_does_not_affect_hash(self):
        a = {"signal_date": "2026-04-06", "direction": "long", "percentile": 0.80}
        b = {"percentile": 0.80, "direction": "long", "signal_date": "2026-04-06"}
        assert ai_cache.hash_trigger_data(a) == ai_cache.hash_trigger_data(b)

    def test_different_percentile_produces_different_hash(self):
        """The PR I.4 motivation: a regime multiplier shift changes
        `trigger_data["percentile"]` → cache key MUST diverge."""
        base = {"signal_date": "2026-04-06", "direction": "long", "percentile": 0.80}
        shifted = {**base, "percentile": 0.88}
        assert ai_cache.hash_trigger_data(base) != ai_cache.hash_trigger_data(shifted)

    def test_handles_decimal_values(self):
        td = {"threshold_usd": Decimal("12345.67"), "percentile": 0.80}
        # Must not raise — default=str in the canonicaliser handles Decimal.
        h = ai_cache.hash_trigger_data(td)
        assert len(h) == 16


class TestGetMiss:
    def test_returns_none_when_file_missing(self):
        assert (
            ai_cache.get(fingerprint="deadbeef00000000000000000000beef", trigger_hash=_H1) is None
        )

    def test_returns_none_when_file_malformed(self, tmp_path: Path):
        fp = "deadbeef00000000000000000000beef"
        path = tmp_path / "v3" / f"{fp}_{_H1}.json"
        path.parent.mkdir(parents=True)
        path.write_text("{not json")
        assert ai_cache.get(fingerprint=fp, trigger_hash=_H1) is None

    def test_returns_none_when_shape_invalid(self, tmp_path: Path):
        fp = "deadbeef00000000000000000000beef"
        path = tmp_path / "v3" / f"{fp}_{_H1}.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"headline": "x"}))
        assert ai_cache.get(fingerprint=fp, trigger_hash=_H1) is None


class TestRoundTrip:
    def test_put_then_get_returns_equivalent_analysis(self):
        analysis = _make_analysis()
        ai_cache.put(fingerprint="a" * 32, trigger_hash=_H1, analysis=analysis)
        result = ai_cache.get(fingerprint="a" * 32, trigger_hash=_H1)
        assert result is not None
        assert result.model_dump() == analysis.model_dump()

    def test_put_writes_under_prompt_version_and_trigger_hash(self, tmp_path: Path):
        ai_cache.put(
            fingerprint="a" * 32,
            trigger_hash=_H1,
            analysis=_make_analysis(),
            prompt_version="v3",
        )
        assert (tmp_path / "v3" / f"{'a' * 32}_{_H1}.json").is_file()

    def test_different_versions_are_isolated(self, tmp_path: Path):
        a1 = _make_analysis(confidence=3)
        a2 = _make_analysis(confidence=9)
        ai_cache.put(fingerprint="b" * 32, trigger_hash=_H1, analysis=a1, prompt_version="v2")
        ai_cache.put(fingerprint="b" * 32, trigger_hash=_H1, analysis=a2, prompt_version="v3")
        got_v3 = ai_cache.get(fingerprint="b" * 32, trigger_hash=_H1, prompt_version="v3")
        got_v2 = ai_cache.get(fingerprint="b" * 32, trigger_hash=_H1, prompt_version="v2")
        assert got_v3 is not None and got_v3.confidence == 9
        assert got_v2 is not None and got_v2.confidence == 3

    def test_different_trigger_hashes_are_isolated(self):
        """PR I.4 contract: same fingerprint, different trigger_hash →
        cache MISS. Two distinct entries can coexist."""
        a1 = _make_analysis(confidence=3)
        a2 = _make_analysis(confidence=9)
        ai_cache.put(fingerprint="c" * 32, trigger_hash=_H1, analysis=a1)
        ai_cache.put(fingerprint="c" * 32, trigger_hash=_H2, analysis=a2)
        got1 = ai_cache.get(fingerprint="c" * 32, trigger_hash=_H1)
        got2 = ai_cache.get(fingerprint="c" * 32, trigger_hash=_H2)
        assert got1 is not None and got1.confidence == 3
        assert got2 is not None and got2.confidence == 9


class TestPathSafety:
    def test_rejects_path_traversal_in_fingerprint(self):
        with pytest.raises(ValueError, match="illegal path chars"):
            ai_cache.get(fingerprint="../escape", trigger_hash=_H1)

    def test_rejects_path_traversal_in_trigger_hash(self):
        with pytest.raises(ValueError, match="illegal path chars"):
            ai_cache.get(fingerprint="a" * 32, trigger_hash="../escape")

    def test_rejects_path_traversal_in_version(self):
        with pytest.raises(ValueError, match="illegal path chars"):
            ai_cache.get(fingerprint="a" * 32, trigger_hash=_H1, prompt_version="../v3")

    def test_rejects_empty_fingerprint(self):
        with pytest.raises(ValueError):
            ai_cache.get(fingerprint="", trigger_hash=_H1)

    def test_rejects_empty_trigger_hash(self):
        with pytest.raises(ValueError):
            ai_cache.get(fingerprint="a" * 32, trigger_hash="")


class TestClear:
    def test_clear_specific_version_removes_only_that_dir(self, tmp_path: Path):
        ai_cache.put(
            fingerprint="c" * 32, trigger_hash=_H1, analysis=_make_analysis(), prompt_version="v2"
        )
        ai_cache.put(
            fingerprint="d" * 32, trigger_hash=_H1, analysis=_make_analysis(), prompt_version="v3"
        )
        removed = ai_cache.clear(prompt_version="v2")
        assert removed == 1
        assert ai_cache.get(fingerprint="c" * 32, trigger_hash=_H1, prompt_version="v2") is None
        assert ai_cache.get(fingerprint="d" * 32, trigger_hash=_H1, prompt_version="v3") is not None

    def test_clear_all_versions(self):
        ai_cache.put(
            fingerprint="e" * 32, trigger_hash=_H1, analysis=_make_analysis(), prompt_version="v2"
        )
        ai_cache.put(
            fingerprint="f" * 32, trigger_hash=_H1, analysis=_make_analysis(), prompt_version="v3"
        )
        removed = ai_cache.clear()
        assert removed == 2

    def test_clear_on_missing_root_is_zero_not_error(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(ai_cache, "CACHE_ROOT", tmp_path / "does_not_exist")
        assert ai_cache.clear() == 0
        assert ai_cache.clear(prompt_version="v3") == 0


class TestAtomicWrite:
    def test_no_temp_files_left_after_put(self, tmp_path: Path):
        ai_cache.put(fingerprint="9" * 32, trigger_hash=_H1, analysis=_make_analysis())
        tmp_files = list(tmp_path.rglob(".tmp_*"))
        assert tmp_files == []
