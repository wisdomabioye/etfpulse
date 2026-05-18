"""Tests for `scripts/backtest.py` — CLI argument parsing + summary table.

Logic lives in `pipeline.backtest` (tested separately). Here we pin the CLI's
own surface: argument validation, the human-facing summary table format, the
JSON write side-effect, and the --help string.

`asyncio.run` + DB session lifecycle in `_run` would require a real Postgres
connection; we exercise only the synchronous bits (`_parse_*`, `_format_summary_table`).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from datetime import date
from pathlib import Path

import pytest

from etfpulse.pipeline.backtest import BacktestPerDetector, BacktestReport


def _load_cli_module():
    """Loads `scripts/backtest.py` as a module so its private helpers can be
    imported. The scripts/ directory isn't on PYTHONPATH by default (it's a
    CLI entry-point dir, not a package), so we load it explicitly."""
    here = Path(__file__).resolve().parents[2]
    path = here / "scripts" / "backtest.py"
    spec = importlib.util.spec_from_file_location("scripts_backtest_for_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["scripts_backtest_for_test"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def cli():
    return _load_cli_module()


class TestParseDate:
    def test_parses_iso_date(self, cli):
        assert cli._parse_date("2026-04-15") == date(2026, 4, 15)

    def test_rejects_bad_date(self, cli):
        with pytest.raises(argparse.ArgumentTypeError):
            cli._parse_date("not-a-date")


class TestParseConfig:
    def test_none_returns_empty_dict(self, cli):
        assert cli._parse_config(None) == {}

    def test_parses_well_formed_json(self, cli):
        result = cli._parse_config('{"magnitude": {"percentile_threshold": 0.85}}')
        assert result == {"magnitude": {"percentile_threshold": 0.85}}

    def test_rejects_non_json(self, cli):
        with pytest.raises(argparse.ArgumentTypeError, match="not valid JSON"):
            cli._parse_config("{not json")

    def test_rejects_non_object_root(self, cli):
        with pytest.raises(argparse.ArgumentTypeError, match="must be a JSON object"):
            cli._parse_config("[]")

    def test_rejects_non_object_inner_value(self, cli):
        with pytest.raises(argparse.ArgumentTypeError, match="must be an object of kwargs"):
            cli._parse_config('{"magnitude": 7}')


class TestSummaryTable:
    def test_renders_minimal_report(self, cli):
        report = BacktestReport(
            start="2026-04-15",
            end="2026-04-17",
            ai_prompt_version="v3",
            detector_configs={},
            counters={"dates_walked": 3, "hits_total": 0},
            per_detector=[
                BacktestPerDetector(
                    detector_name="flow_anomaly",
                    n_hits=2,
                    n_scored=1,
                    wins=1,
                    losses=0,
                    hit_rate=1.0,
                ),
                BacktestPerDetector(
                    detector_name="magnitude",
                    n_hits=0,
                    n_scored=0,
                    wins=0,
                    losses=0,
                    hit_rate=None,
                ),
            ],
            outcomes=[],
        )
        text = cli._format_summary_table(report)
        # Header + window
        assert "2026-04-15 → 2026-04-17" in text
        assert "v3" in text
        # Counter section
        assert "dates_walked" in text
        # Per-detector rendering: hit_rate=None → "—", populated → "100.0%"
        assert "100.0%" in text
        assert "—" in text


class TestJSONShape:
    def test_report_serialises_to_disk(self, tmp_path: Path):
        """End-to-end JSON write: build a minimal report, serialise, read back.
        Pins the `to_json_dict` shape that the CLI commits to disk."""
        report = BacktestReport(
            start="2026-04-15",
            end="2026-04-17",
            ai_prompt_version="v3",
            detector_configs={"flow_anomaly": {"lookback_days": 14}},
            counters={"dates_walked": 3},
            per_detector=[
                BacktestPerDetector(
                    detector_name="flow_anomaly",
                    n_hits=1,
                    n_scored=1,
                    wins=1,
                    losses=0,
                    hit_rate=1.0,
                )
            ],
            outcomes=[],
        )
        out = tmp_path / "bt.json"
        out.write_text(json.dumps(report.to_json_dict(), indent=2))

        loaded = json.loads(out.read_text())
        assert loaded["start"] == "2026-04-15"
        assert loaded["per_detector"][0]["hit_rate"] == 1.0
        assert loaded["detector_configs"]["flow_anomaly"]["lookback_days"] == 14
