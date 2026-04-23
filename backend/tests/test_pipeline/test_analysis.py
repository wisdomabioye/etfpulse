"""AISignalAnalysis schema — clamping, truncation, and expiry mapping.

The validators are forgiving by design (R18) — these tests pin the exact
"forgiving" behaviour so a future strict-mode refactor doesn't quietly
change the contract.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from etfpulse.pipeline.analysis import AISignalAnalysis, compute_expires_at


def _base(**overrides) -> dict:
    """Minimum-valid payload, override any field per test."""
    return {
        "headline": "BTC inflows snap 4-day streak",
        "reasoning": ["Streak broken", "Volume spike"],
        "confidence": 7,
        "risks": ["Macro headwind"],
        "suggested_action": "consider short",
        "time_horizon": "swing",
    } | overrides


class TestConfidenceClamp:
    def test_in_range(self):
        assert AISignalAnalysis(**_base(confidence=5)).confidence == 5

    def test_below_min_clamps_to_one(self):
        assert AISignalAnalysis(**_base(confidence=-3)).confidence == 1
        assert AISignalAnalysis(**_base(confidence=0)).confidence == 1

    def test_above_max_clamps_to_ten(self):
        assert AISignalAnalysis(**_base(confidence=11)).confidence == 10
        assert AISignalAnalysis(**_base(confidence=999)).confidence == 10

    def test_string_int_coerces_then_clamps(self):
        # LLMs occasionally JSON-encode numbers as strings.
        assert AISignalAnalysis(**_base(confidence="15")).confidence == 10

    def test_unparseable_string_falls_back_to_one(self):
        # Garbage input doesn't 422 the whole response — degrade gracefully.
        assert AISignalAnalysis(**_base(confidence="banana")).confidence == 1


class TestHeadlineTrim:
    def test_strips_whitespace(self):
        a = AISignalAnalysis(**_base(headline="  Headline  "))
        assert a.headline == "Headline"

    def test_long_headline_truncates_with_ellipsis(self):
        long = "X" * 250
        a = AISignalAnalysis(**_base(headline=long))
        assert len(a.headline) == 200  # cap inclusive of the ellipsis
        assert a.headline.endswith("…")

    def test_at_cap_unchanged(self):
        exactly = "X" * 200
        a = AISignalAnalysis(**_base(headline=exactly))
        assert a.headline == exactly


class TestReasoningCleanup:
    def test_strips_and_drops_empties(self):
        a = AISignalAnalysis(**_base(reasoning=["  real ", "", "   ", "another"]))
        assert a.reasoning == ["real", "another"]

    def test_truncates_at_five(self):
        a = AISignalAnalysis(**_base(reasoning=[f"r{i}" for i in range(10)]))
        assert len(a.reasoning) == 5
        assert a.reasoning == ["r0", "r1", "r2", "r3", "r4"]

    def test_empty_after_cleanup_allowed(self):
        # `reasoning` field type is list[str] — no min_length. An LLM that
        # returns only blanks gets an empty list, not an error.
        a = AISignalAnalysis(**_base(reasoning=["", "  "]))
        assert a.reasoning == []

    def test_drop_then_truncate_order(self):
        # Five blanks followed by a real entry — if order were reversed
        # (truncate first), the real entry would be dropped. Pin behaviour.
        a = AISignalAnalysis(**_base(reasoning=["", "", "", "", "", "real"]))
        assert a.reasoning == ["real"]


class TestRisksCleanup:
    def test_truncates_at_three(self):
        a = AISignalAnalysis(**_base(risks=["a", "b", "c", "d", "e"]))
        assert a.risks == ["a", "b", "c"]


class TestEnumeratedFields:
    def test_invalid_suggested_action_rejected(self):
        # Literal fields ARE strict — the LLM must return one of the three.
        with pytest.raises(ValidationError):
            AISignalAnalysis(**_base(suggested_action="moon"))

    def test_invalid_time_horizon_rejected(self):
        with pytest.raises(ValidationError):
            AISignalAnalysis(**_base(time_horizon="forever"))


class TestExtraFieldsIgnored:
    def test_extra_fields_dropped_silently(self):
        # LLMs sometimes add chatty keys; we don't want strict mode here.
        a = AISignalAnalysis(**_base(thinking="...", caveat="..."))
        assert not hasattr(a, "thinking")


class TestComputeExpiresAt:
    @pytest.mark.parametrize(
        "horizon,delta",
        [
            ("scalp", timedelta(hours=6)),
            ("swing", timedelta(hours=72)),
            ("position", timedelta(days=7)),
        ],
    )
    def test_known_horizons(self, horizon, delta):
        now = datetime(2026, 4, 23, 12, 0, tzinfo=UTC)
        assert compute_expires_at(horizon, now=now) == now + delta

    def test_default_now_is_utc(self):
        # No `now` arg → uses current UTC. We can't pin the value, but we can
        # assert the result is timezone-aware UTC.
        result = compute_expires_at("scalp")
        assert result.tzinfo is not None
        assert result.tzinfo.utcoffset(result) == timedelta(0)

    def test_unknown_horizon_raises(self):
        with pytest.raises(ValueError, match="unknown time_horizon"):
            compute_expires_at("forever")


class TestFixtureFile:
    """Sanity-check the canned analyses ship as valid AISignalAnalysis.

    Demo mode (`sosovalue_use_fixtures=True`) reads this file straight into the
    schema. If the fixture drifts (added field, typo in literal value, count
    over the cap), demo mode silently returns None — this test fails loudly
    instead.
    """

    def test_every_entry_validates(self):
        import json
        from pathlib import Path

        fixture = (
            Path(__file__).resolve().parent.parent.parent / "fixtures" / "openrouter_analysis.json"
        )
        data = json.loads(fixture.read_text())

        # Three Stage 4a/4b detector signal_types must each have an entry.
        assert set(data.keys()) >= {"flow_anomaly", "magnitude", "acceleration"}

        for signal_type, payload in data.items():
            analysis = AISignalAnalysis.model_validate(payload)
            assert analysis.headline, f"{signal_type} has empty headline after validation"
