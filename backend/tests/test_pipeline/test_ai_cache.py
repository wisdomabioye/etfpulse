"""Tests for `pipeline.ai_cache` — file-based AI response cache (PR I.5).

We isolate every test from on-disk state by monkey-patching `CACHE_ROOT` to
a per-test `tmp_path`. The real cache root (`backend/.backtest_cache/`) is
never touched by tests.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from etfpulse.pipeline import ai_cache
from etfpulse.pipeline.analysis import AISignalAnalysis


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
    """Point `CACHE_ROOT` at a tmpdir for every test in this module.

    `_key_path` reads the module-level `CACHE_ROOT` constant, so a
    `monkeypatch.setattr` on the module attribute is sufficient — no need
    to re-import.
    """
    monkeypatch.setattr(ai_cache, "CACHE_ROOT", tmp_path)
    return tmp_path


class TestGetMiss:
    def test_returns_none_when_file_missing(self):
        assert ai_cache.get(fingerprint="deadbeef00000000000000000000beef") is None

    def test_returns_none_when_file_malformed(self, tmp_path: Path):
        path = tmp_path / "v3" / "deadbeef00000000000000000000beef.json"
        path.parent.mkdir(parents=True)
        path.write_text("{not json")
        assert ai_cache.get(fingerprint="deadbeef00000000000000000000beef") is None

    def test_returns_none_when_shape_invalid(self, tmp_path: Path):
        path = tmp_path / "v3" / "deadbeef00000000000000000000beef.json"
        path.parent.mkdir(parents=True)
        # Valid JSON but not a valid AISignalAnalysis (missing required fields).
        path.write_text(json.dumps({"headline": "x"}))
        assert ai_cache.get(fingerprint="deadbeef00000000000000000000beef") is None


class TestRoundTrip:
    def test_put_then_get_returns_equivalent_analysis(self):
        analysis = _make_analysis()
        ai_cache.put(fingerprint="a" * 32, analysis=analysis)
        result = ai_cache.get(fingerprint="a" * 32)
        assert result is not None
        # Compare field by field — Pydantic equality through model_dump.
        assert result.model_dump() == analysis.model_dump()

    def test_put_writes_under_prompt_version_subdir(self, tmp_path: Path):
        ai_cache.put(fingerprint="a" * 32, analysis=_make_analysis(), prompt_version="v3")
        assert (tmp_path / "v3" / ("a" * 32 + ".json")).is_file()

    def test_different_versions_are_isolated(self, tmp_path: Path):
        a1 = _make_analysis(confidence=3)
        a2 = _make_analysis(confidence=9)
        ai_cache.put(fingerprint="b" * 32, analysis=a1, prompt_version="v2")
        ai_cache.put(fingerprint="b" * 32, analysis=a2, prompt_version="v3")
        # v3 lookup gets v3 content — prompt-version is the invalidation seam.
        got_v3 = ai_cache.get(fingerprint="b" * 32, prompt_version="v3")
        got_v2 = ai_cache.get(fingerprint="b" * 32, prompt_version="v2")
        assert got_v3 is not None and got_v3.confidence == 9
        assert got_v2 is not None and got_v2.confidence == 3


class TestPathSafety:
    def test_rejects_path_traversal_in_fingerprint(self):
        with pytest.raises(ValueError, match="illegal path chars"):
            ai_cache.get(fingerprint="../escape")

    def test_rejects_path_traversal_in_version(self):
        with pytest.raises(ValueError, match="illegal path chars"):
            ai_cache.get(fingerprint="a" * 32, prompt_version="../v3")

    def test_rejects_empty_fingerprint(self):
        with pytest.raises(ValueError):
            ai_cache.get(fingerprint="")


class TestClear:
    def test_clear_specific_version_removes_only_that_dir(self, tmp_path: Path):
        ai_cache.put(fingerprint="c" * 32, analysis=_make_analysis(), prompt_version="v2")
        ai_cache.put(fingerprint="d" * 32, analysis=_make_analysis(), prompt_version="v3")
        removed = ai_cache.clear(prompt_version="v2")
        assert removed == 1
        assert ai_cache.get(fingerprint="c" * 32, prompt_version="v2") is None
        assert ai_cache.get(fingerprint="d" * 32, prompt_version="v3") is not None

    def test_clear_all_versions(self):
        ai_cache.put(fingerprint="e" * 32, analysis=_make_analysis(), prompt_version="v2")
        ai_cache.put(fingerprint="f" * 32, analysis=_make_analysis(), prompt_version="v3")
        removed = ai_cache.clear()
        assert removed == 2

    def test_clear_on_missing_root_is_zero_not_error(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(ai_cache, "CACHE_ROOT", tmp_path / "does_not_exist")
        assert ai_cache.clear() == 0
        assert ai_cache.clear(prompt_version="v3") == 0


class TestAtomicWrite:
    def test_no_temp_files_left_after_put(self, tmp_path: Path):
        ai_cache.put(fingerprint="9" * 32, analysis=_make_analysis())
        tmp_files = list(tmp_path.rglob(".tmp_*"))
        assert tmp_files == []
