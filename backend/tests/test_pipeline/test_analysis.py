"""AISignalAnalysis schema — clamping, truncation, and expiry mapping.

The validators are forgiving by design (R18) — these tests pin the exact
"forgiving" behaviour so a future strict-mode refactor doesn't quietly
change the contract.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from etfpulse.pipeline.analysis import AI_PROMPT_VERSION, AISignalAnalysis, compute_expires_at


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

    def test_directional_entries_carry_price_levels(self):
        """Stage 8-P1 — every fixture with suggested_action != 'wait' must
        carry entry/stop/target (otherwise demo mode produces signals with
        no actionable levels and the OutcomeCard renders empty)."""
        import json
        from pathlib import Path

        fixture = (
            Path(__file__).resolve().parent.parent.parent / "fixtures" / "openrouter_analysis.json"
        )
        data = json.loads(fixture.read_text())

        for signal_type, payload in data.items():
            analysis = AISignalAnalysis.model_validate(payload)
            if analysis.suggested_action == "wait":
                assert analysis.entry_price is None, (
                    f"{signal_type} wait-suggestion has entry_price"
                )
                assert analysis.stop_price is None
                assert analysis.target_price is None
            else:
                assert analysis.entry_price is not None, f"{signal_type} missing entry_price"
                assert analysis.stop_price is not None, f"{signal_type} missing stop_price"
                assert analysis.target_price is not None, f"{signal_type} missing target_price"
                assert analysis.entry_price > 0
                assert analysis.stop_price > 0
                assert analysis.target_price > 0


class TestPromptVersion:
    """Stage 8-P1 bumped v2→v3. Pin the version + the bump rationale here so
    a regression that resets it loudly fails the build instead of silently
    breaking the track-record query."""

    def test_version_matches_v3(self):
        assert AI_PROMPT_VERSION == "v3"

    def test_version_format_matches_db_check_constraint(self):
        # The DB has CHECK (ai_prompt_version ~ '^v[0-9]+$') — pin it here too
        # so a future bump (e.g. "v3.1") fails the test before hitting the DB.
        assert re.fullmatch(r"v[0-9]+", AI_PROMPT_VERSION)


class TestSuggestedPriceLevels:
    """Stage 8-P1 — entry/stop/target validators."""

    def test_directional_with_prices_persists(self):
        a = AISignalAnalysis(
            **_base(
                suggested_action="consider long",
                entry_price=Decimal("84200"),
                stop_price=Decimal("82000"),
                target_price=Decimal("89500"),
            )
        )
        assert a.entry_price == Decimal("84200")
        assert a.stop_price == Decimal("82000")
        assert a.target_price == Decimal("89500")

    def test_string_decimals_coerce(self):
        # LLMs sometimes JSON-encode prices as strings.
        a = AISignalAnalysis(
            **_base(
                suggested_action="consider short",
                entry_price="84200.50",
                stop_price="85800.25",
                target_price="81500.75",
            )
        )
        assert a.entry_price == Decimal("84200.50")
        assert a.stop_price == Decimal("85800.25")
        assert a.target_price == Decimal("81500.75")

    def test_int_and_float_coerce_to_decimal(self):
        a = AISignalAnalysis(
            **_base(
                suggested_action="consider long",
                entry_price=84200,
                stop_price=82000.5,
                target_price=89500,
            )
        )
        assert a.entry_price == Decimal("84200")
        assert isinstance(a.stop_price, Decimal)
        assert a.stop_price == Decimal("82000.5")

    def test_negative_prices_become_none(self):
        # Forgiving — degrade rather than 422 the whole analysis.
        a = AISignalAnalysis(
            **_base(
                suggested_action="consider long",
                entry_price=-100,
                stop_price=Decimal("-50"),
                target_price=Decimal("0"),
            )
        )
        assert a.entry_price is None
        assert a.stop_price is None
        assert a.target_price is None

    def test_unparseable_price_becomes_none(self):
        a = AISignalAnalysis(
            **_base(
                suggested_action="consider long",
                entry_price="banana",
                stop_price=None,
                target_price=Decimal("89500"),
            )
        )
        assert a.entry_price is None
        assert a.stop_price is None
        assert a.target_price == Decimal("89500")

    def test_nan_and_infinity_become_none(self):
        """Postgres NUMERIC rejects NaN/Infinity on INSERT, AND `Decimal('NaN')
        <= 0` raises decimal.InvalidOperation. Both must short-circuit to
        None before the comparison runs."""
        a = AISignalAnalysis(
            **_base(
                suggested_action="consider long",
                entry_price="NaN",
                stop_price="Infinity",
                target_price="-Infinity",
            )
        )
        assert a.entry_price is None
        assert a.stop_price is None
        assert a.target_price is None

    def test_wait_drops_any_volunteered_prices(self):
        """A 'wait' suggestion implies no actionable trade — even if the
        model volunteered numbers, drop them. Otherwise the Telegram
        formatter would render 'Suggested: WAIT at $84,200'."""
        a = AISignalAnalysis(
            **_base(
                suggested_action="wait",
                entry_price=Decimal("84200"),
                stop_price=Decimal("82000"),
                target_price=Decimal("89500"),
            )
        )
        assert a.entry_price is None
        assert a.stop_price is None
        assert a.target_price is None

    def test_directional_without_prices_still_valid(self):
        # A model that genuinely declines to volunteer levels for a weak
        # signal must not 422 the analysis — the headline + reasoning are
        # still useful on their own.
        a = AISignalAnalysis(**_base(suggested_action="consider long"))
        assert a.entry_price is None
        assert a.stop_price is None
        assert a.target_price is None
        assert a.suggested_action == "consider long"

    def test_v3_schema_includes_price_fields_in_json_schema(self):
        """Pins the wire-shape the prompt embeds — `_build_messages` calls
        `AISignalAnalysis.model_json_schema()` to teach the LLM the response
        shape. If a refactor accidentally drops the new fields, the LLM
        stops returning them and the test fails before prod."""
        schema = AISignalAnalysis.model_json_schema()
        properties = schema.get("properties", {})
        assert "entry_price" in properties
        assert "stop_price" in properties
        assert "target_price" in properties
