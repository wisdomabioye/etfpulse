"""OpenRouter adapter — happy path, error classes, daily-cap enforcement.

R6 invariant: `analyze()` NEVER raises. Every test that exercises a failure
mode asserts the call returns `None`, not an exception.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from etfpulse.adapters.openrouter import OpenRouterClient
from etfpulse.config import settings
from etfpulse.models import MarketRegime, SignalPosture
from etfpulse.pipeline.regime_monitor import RegimeClassification

_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"

_VALID_ANALYSIS = {
    "headline": "BTC inflows snap 4-day streak",
    "reasoning": ["Streak broken", "Volume spike"],
    "confidence": 7,
    "risks": ["Macro headwind"],
    "suggested_action": "consider short",
    "time_horizon": "swing",
}


def _api_response(content: dict) -> dict:
    """OpenRouter chat-completions envelope wrapping a JSON-string content."""
    return {
        "id": "gen-test",
        "model": "anthropic/claude-sonnet-4.6",
        "choices": [{"message": {"role": "assistant", "content": json.dumps(content)}}],
    }


@pytest.fixture
def live_client(monkeypatch):
    """Client configured for HTTP, not fixtures, with a real-looking API key."""
    monkeypatch.setattr(settings, "sosovalue_use_fixtures", False)
    monkeypatch.setattr(settings, "openrouter_api_key", "sk-or-v1-test")
    return OpenRouterClient()


# ---- Happy path -----------------------------------------------------------


async def test_happy_path_returns_validated_analysis(httpx_mock, live_client):
    httpx_mock.add_response(url=_CHAT_URL, json=_api_response(_VALID_ANALYSIS))

    result = await live_client.analyze(
        signal_type="flow_anomaly",
        asset="BTC",
        trigger_data={"streak_length": 4, "streak_direction": "long"},
    )

    assert result is not None
    assert result.headline == "BTC inflows snap 4-day streak"
    assert result.confidence == 7
    assert result.suggested_action == "consider short"


async def test_happy_path_consumes_one_call_from_cap(httpx_mock, live_client):
    httpx_mock.add_response(url=_CHAT_URL, json=_api_response(_VALID_ANALYSIS))
    assert live_client._call_count == 0
    await live_client.analyze("flow_anomaly", "BTC", {})
    assert live_client._call_count == 1


# ---- Failure modes (R6 — all return None, never raise) ---------------------


async def test_429_returns_none(httpx_mock, live_client):
    httpx_mock.add_response(url=_CHAT_URL, status_code=429, json={"error": "rate limited"})
    result = await live_client.analyze("flow_anomaly", "BTC", {})
    assert result is None


async def test_500_returns_none(httpx_mock, live_client):
    httpx_mock.add_response(url=_CHAT_URL, status_code=500, text="Internal Server Error")
    result = await live_client.analyze("flow_anomaly", "BTC", {})
    assert result is None


async def test_failure_still_increments_counter(httpx_mock, live_client):
    """Bumping BEFORE the call means a flapping API can't blow past the cap."""
    httpx_mock.add_response(url=_CHAT_URL, status_code=500)
    await live_client.analyze("flow_anomaly", "BTC", {})
    assert live_client._call_count == 1


async def test_malformed_json_in_content_returns_none(httpx_mock, live_client):
    bad_envelope = {
        "choices": [{"message": {"role": "assistant", "content": "not json {{{"}}],
    }
    httpx_mock.add_response(url=_CHAT_URL, json=bad_envelope)
    result = await live_client.analyze("flow_anomaly", "BTC", {})
    assert result is None


async def test_schema_mismatch_returns_none(httpx_mock, live_client):
    """Valid JSON but missing required field — still returns None."""
    bad_payload = {"headline": "X", "reasoning": []}  # missing everything else
    httpx_mock.add_response(url=_CHAT_URL, json=_api_response(bad_payload))
    result = await live_client.analyze("flow_anomaly", "BTC", {})
    assert result is None


async def test_no_choices_in_envelope_returns_none(httpx_mock, live_client):
    httpx_mock.add_response(url=_CHAT_URL, json={"choices": []})
    result = await live_client.analyze("flow_anomaly", "BTC", {})
    assert result is None


async def test_missing_api_key_returns_none(monkeypatch, httpx_mock):
    """No API key → no HTTP call (httpx_mock would assert on it), just None."""
    monkeypatch.setattr(settings, "sosovalue_use_fixtures", False)
    monkeypatch.setattr(settings, "openrouter_api_key", "")
    client = OpenRouterClient()
    result = await client.analyze("flow_anomaly", "BTC", {})
    assert result is None
    # Counter must NOT bump when we short-circuited on missing key.
    assert client._call_count == 0


# ---- Daily cap -------------------------------------------------------------


async def test_daily_cap_exceeded_returns_none(monkeypatch, httpx_mock):
    monkeypatch.setattr(settings, "sosovalue_use_fixtures", False)
    monkeypatch.setattr(settings, "openrouter_api_key", "sk-or-v1-test")
    monkeypatch.setattr(settings, "openrouter_daily_call_cap", 2)

    client = OpenRouterClient()
    httpx_mock.add_response(url=_CHAT_URL, json=_api_response(_VALID_ANALYSIS))
    httpx_mock.add_response(url=_CHAT_URL, json=_api_response(_VALID_ANALYSIS))

    # Two calls succeed (within cap)
    assert await client.analyze("flow_anomaly", "BTC", {}) is not None
    assert await client.analyze("flow_anomaly", "BTC", {}) is not None
    # Third call hits the cap → None, no HTTP request issued (would error otherwise)
    assert await client.analyze("flow_anomaly", "BTC", {}) is None
    assert client._call_count == 2


async def test_cap_zero_disables_enforcement(monkeypatch, httpx_mock):
    monkeypatch.setattr(settings, "sosovalue_use_fixtures", False)
    monkeypatch.setattr(settings, "openrouter_api_key", "sk-or-v1-test")
    monkeypatch.setattr(settings, "openrouter_daily_call_cap", 0)

    client = OpenRouterClient()
    for _ in range(5):
        httpx_mock.add_response(url=_CHAT_URL, json=_api_response(_VALID_ANALYSIS))
        assert await client.analyze("flow_anomaly", "BTC", {}) is not None


async def test_counter_resets_on_utc_day_rollover(monkeypatch, httpx_mock):
    monkeypatch.setattr(settings, "sosovalue_use_fixtures", False)
    monkeypatch.setattr(settings, "openrouter_api_key", "sk-or-v1-test")
    monkeypatch.setattr(settings, "openrouter_daily_call_cap", 1)

    client = OpenRouterClient()
    httpx_mock.add_response(url=_CHAT_URL, json=_api_response(_VALID_ANALYSIS))

    # First call eats the only slot.
    assert await client.analyze("flow_anomaly", "BTC", {}) is not None

    # Simulate UTC day rollover by patching the static helper.
    tomorrow = (datetime.now(UTC) + timedelta(days=1)).date()
    monkeypatch.setattr(OpenRouterClient, "_utc_today", staticmethod(lambda: tomorrow))

    # Counter resets — new call goes through.
    httpx_mock.add_response(url=_CHAT_URL, json=_api_response(_VALID_ANALYSIS))
    assert await client.analyze("flow_anomaly", "BTC", {}) is not None
    assert client._call_count == 1  # reset to 0, then +1 for this call


# ---- Fixture mode ---------------------------------------------------------


async def test_fixture_mode_missing_file_returns_none(monkeypatch, tmp_path):
    """If the fixture file doesn't exist, returns None — no HTTP attempted."""
    monkeypatch.setattr(settings, "sosovalue_use_fixtures", True)
    # Point the adapter at an empty dir so the fixture file is missing.
    monkeypatch.setattr("etfpulse.adapters.openrouter.FIXTURES_DIR", tmp_path)

    client = OpenRouterClient()
    result = await client.analyze("flow_anomaly", "BTC", {})
    assert result is None


async def test_fixture_mode_missing_signal_key_returns_none(monkeypatch, tmp_path):
    """Fixture file exists but lacks the requested signal_type → None."""
    monkeypatch.setattr(settings, "sosovalue_use_fixtures", True)
    monkeypatch.setattr("etfpulse.adapters.openrouter.FIXTURES_DIR", tmp_path)

    (tmp_path / "openrouter_analysis.json").write_text(json.dumps({"magnitude": _VALID_ANALYSIS}))

    client = OpenRouterClient()
    result = await client.analyze("flow_anomaly", "BTC", {})
    assert result is None


async def test_fixture_mode_present_key_returns_analysis(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "sosovalue_use_fixtures", True)
    monkeypatch.setattr("etfpulse.adapters.openrouter.FIXTURES_DIR", tmp_path)

    (tmp_path / "openrouter_analysis.json").write_text(
        json.dumps({"flow_anomaly": _VALID_ANALYSIS})
    )

    client = OpenRouterClient()
    result = await client.analyze("flow_anomaly", "BTC", {})
    assert result is not None
    assert result.headline == "BTC inflows snap 4-day streak"
    # Fixture mode does NOT touch the daily counter.
    assert client._call_count == 0


# ---- Stage 7-P6: prompt v2 + finish_reason='error' + identifying headers ----


async def test_finish_reason_error_returns_none(httpx_mock, live_client):
    """Mid-200 provider error per OpenRouter docs — `choices[0].finish_reason
    == 'error'` paired with a populated `error` field. Must be caught and
    returned as None per R6, NOT propagated as an exception."""
    httpx_mock.add_response(
        url=_CHAT_URL,
        json={
            "id": "gen-mid-error",
            "model": "anthropic/claude-sonnet-4.6",
            "choices": [
                {
                    "finish_reason": "error",
                    "message": {"role": "assistant", "content": None},
                    "error": {"code": 502, "message": "upstream provider down"},
                }
            ],
        },
    )

    result = await live_client.analyze("flow_anomaly", "BTC", {})
    assert result is None


async def test_identifying_headers_sent(httpx_mock, live_client, monkeypatch):
    """Adapter must send HTTP-Referer + X-OpenRouter-Title so OpenRouter can
    attribute usage. Verified via httpx_mock's recorded request."""
    monkeypatch.setattr(settings, "openrouter_app_url", "https://example.test")
    monkeypatch.setattr(settings, "openrouter_app_title", "ETFPulseTest")

    httpx_mock.add_response(url=_CHAT_URL, json=_api_response(_VALID_ANALYSIS))
    await live_client.analyze("flow_anomaly", "BTC", {})

    requests = httpx_mock.get_requests()
    assert len(requests) == 1
    headers = requests[0].headers
    assert headers["HTTP-Referer"] == "https://example.test"
    assert headers["X-OpenRouter-Title"] == "ETFPulseTest"


async def test_v2_prompt_includes_regime_when_supplied(httpx_mock, live_client):
    """When `regime` kwarg is passed, the user message must contain the
    regime block. Verified by inspecting the outbound request body."""
    classification = RegimeClassification(
        regime=MarketRegime.MARKUP,
        signal_posture=SignalPosture.NORMAL,
        confidence=8,
        reasoning={"score": 50},
        macro_events_nearby=["FOMC"],
    )

    httpx_mock.add_response(url=_CHAT_URL, json=_api_response(_VALID_ANALYSIS))
    await live_client.analyze("flow_anomaly", "BTC", {}, regime=classification, news_context=None)

    body = json.loads(httpx_mock.get_requests()[0].content)
    user_msg = body["messages"][1]["content"]
    assert "Market regime classification" in user_msg
    assert '"regime": "markup"' in user_msg
    assert '"signal_posture": "normal"' in user_msg
    assert '"FOMC"' in user_msg


async def test_v2_prompt_includes_news_context_when_supplied(httpx_mock, live_client):
    """When `news_context` is non-empty, prompt embeds the items as JSON."""
    httpx_mock.add_response(url=_CHAT_URL, json=_api_response(_VALID_ANALYSIS))
    news = [
        {
            "title": "BlackRock files amendment",
            "category": 3,
            "published_iso": "2026-04-30T12:00:00+00:00",
            "summary": "In-kind redemptions clarified.",
        }
    ]

    await live_client.analyze("flow_anomaly", "BTC", {}, news_context=news)

    body = json.loads(httpx_mock.get_requests()[0].content)
    user_msg = body["messages"][1]["content"]
    assert "Recent relevant news" in user_msg
    assert "BlackRock files amendment" in user_msg


async def test_v2_prompt_omits_optional_blocks_when_none(httpx_mock, live_client):
    """Backward-compat: legacy callers (no regime, no news) get a prompt
    that omits both blocks entirely — not empty placeholders."""
    httpx_mock.add_response(url=_CHAT_URL, json=_api_response(_VALID_ANALYSIS))
    await live_client.analyze("flow_anomaly", "BTC", {})

    body = json.loads(httpx_mock.get_requests()[0].content)
    user_msg = body["messages"][1]["content"]
    assert "Market regime classification" not in user_msg
    assert "Recent relevant news" not in user_msg


# ---- v3 prompt (Stage 8-P1) ----------------------------------------------


async def test_v3_system_prompt_describes_price_levels(httpx_mock, live_client):
    """Stage 8-P1 — the system prompt must teach the LLM the rules for
    entry/stop/target. Without the rules the model often returns the
    fields but with arbitrary values, defeating the validator's clamping
    (negative-or-zero → None) and producing many-NULL signals."""
    httpx_mock.add_response(url=_CHAT_URL, json=_api_response(_VALID_ANALYSIS))
    await live_client.analyze("flow_anomaly", "BTC", {})

    body = json.loads(httpx_mock.get_requests()[0].content)
    system_msg = body["messages"][0]["content"]
    assert "PRICE LEVELS" in system_msg
    assert "entry_price" in system_msg
    assert "stop_price" in system_msg
    assert "target_price" in system_msg
    # The "wait → null all three" rule is critical — verify it's mentioned.
    assert "'wait'" in system_msg


async def test_v3_user_prompt_schema_includes_price_fields(httpx_mock, live_client):
    """The embedded JSON schema (built from `AISignalAnalysis.model_json_schema`)
    must surface the new fields so the LLM sees the response shape. This pins
    the auto-generated schema embedding — a refactor that drops the embedding
    or stops calling model_json_schema would silently regress the prompt."""
    httpx_mock.add_response(url=_CHAT_URL, json=_api_response(_VALID_ANALYSIS))
    await live_client.analyze("flow_anomaly", "BTC", {})

    body = json.loads(httpx_mock.get_requests()[0].content)
    user_msg = body["messages"][1]["content"]
    # The schema appears as the trailing JSON block; assert all three fields
    # are textually present (the schema has property names verbatim).
    assert '"entry_price"' in user_msg
    assert '"stop_price"' in user_msg
    assert '"target_price"' in user_msg


async def test_v3_response_with_price_levels_validates_through(httpx_mock, live_client):
    """End-to-end: a v3-shaped response (entry/stop/target included) flows
    through the adapter without losing the price fields."""
    response = {
        **_VALID_ANALYSIS,
        "suggested_action": "consider long",
        "entry_price": "84200.00",
        "stop_price": "82000.00",
        "target_price": "89500.00",
    }
    httpx_mock.add_response(url=_CHAT_URL, json=_api_response(response))

    result = await live_client.analyze("flow_anomaly", "BTC", {})
    assert result is not None
    assert result.entry_price is not None
    assert str(result.entry_price) == "84200.00"
    assert str(result.stop_price) == "82000.00"
    assert str(result.target_price) == "89500.00"


async def test_v3_prompt_includes_current_price_when_supplied(httpx_mock, live_client):
    """Stage 8-P1 — when `current_price` is passed, the user prompt embeds
    a 'Current spot price' line so the LLM has a real anchor for the
    entry/stop/target rules instead of guessing from training data."""
    httpx_mock.add_response(url=_CHAT_URL, json=_api_response(_VALID_ANALYSIS))
    await live_client.analyze("flow_anomaly", "BTC", {}, current_price=Decimal("84250.50"))

    body = json.loads(httpx_mock.get_requests()[0].content)
    user_msg = body["messages"][1]["content"]
    assert "Current spot price (USD): 84250.50" in user_msg


async def test_v3_prompt_omits_current_price_when_none(httpx_mock, live_client):
    """Backward-compat — when `current_price` is omitted (legacy callers,
    or both providers failed at fetch time), the prompt simply skips that
    section. The system prompt's price-level rules instruct the model to
    return null entry/stop/target in that case."""
    httpx_mock.add_response(url=_CHAT_URL, json=_api_response(_VALID_ANALYSIS))
    await live_client.analyze("flow_anomaly", "BTC", {})

    body = json.loads(httpx_mock.get_requests()[0].content)
    user_msg = body["messages"][1]["content"]
    assert "Current spot price" not in user_msg
