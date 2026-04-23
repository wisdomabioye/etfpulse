"""OpenRouter adapter — happy path, error classes, daily-cap enforcement.

R6 invariant: `analyze()` NEVER raises. Every test that exercises a failure
mode asserts the call returns `None`, not an exception.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from etfpulse.adapters.openrouter import OpenRouterClient
from etfpulse.config import settings

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
