"""Unit tests for the SoSoValue adapter — DTO parsing + error classification."""

from __future__ import annotations

from datetime import date

import pytest

from etfpulse.adapters.sosovalue import (
    SoSoValueClient,
    SoSoValueError,
    SoSoValueMonthlyQuotaError,
    sosovalue_client,
)
from etfpulse.config import settings

# ---- Fixture-driven parsing -----------------------------------------------


async def test_etf_flows_parses_and_dedupes_btc():
    """BTC fixture has 10 rows spanning 7 unique dates (2026-04-17 × 3, 2026-04-10 × 2)."""
    flows = await sosovalue_client.get_etf_flows("BTC")

    dates = [f.date for f in flows]
    assert len(dates) == len(set(dates)), "adapter must dedupe by date"
    assert len(flows) == 7

    first = flows[0]
    assert first.date == date(2026, 4, 17)
    # The first row for 2026-04-17 in the fixture has total_net_inflow=663911366.465
    assert float(first.total_net_inflow) == pytest.approx(663911366.465, rel=1e-9)


async def test_etf_flows_parses_eth():
    flows = await sosovalue_client.get_etf_flows("ETH")
    assert len(flows) > 0
    assert all(f.date is not None for f in flows)


async def test_news_parses_fixture_and_strips_html():
    articles = await sosovalue_client.get_news(category=3)
    assert len(articles) >= 2

    first = articles[0]
    # Fixture's first item has title=null; display_title falls back to content.
    assert first.title is None
    assert first.display_title, "display_title must fall back to stripped content"
    assert "<br>" not in first.display_title
    assert "<img" not in first.display_title

    # stripped_content must contain no HTML tags at all
    assert "<" not in first.stripped_content
    assert ">" not in first.stripped_content

    # released_at parses the ms-timestamp string
    assert first.released_at.year >= 2026


async def test_macro_events_parses_fixture():
    events = await sosovalue_client.get_macro_events()
    assert len(events) == 4
    assert events[0].date == date(2026, 4, 20)
    assert events[0].events == ["Retail Sales (MoM)"]


# ---- Caching --------------------------------------------------------------


async def test_etf_flows_are_cached():
    flows_a = await sosovalue_client.get_etf_flows("BTC")
    flows_b = await sosovalue_client.get_etf_flows("BTC")
    # Identity check proves we got the cached list back, not just equal data
    assert flows_a is flows_b


# ---- 429 classification ---------------------------------------------------


async def test_monthly_quota_raises_without_retry(httpx_mock, monkeypatch):
    """429 with a `Monthly quota` message must raise immediately, no retry."""
    monkeypatch.setattr(settings, "sosovalue_use_fixtures", False)
    monkeypatch.setattr(settings, "sosovalue_api_key", "SOSO-test")

    httpx_mock.add_response(
        url=f"{settings.sosovalue_base_url}/macro/events?page=1&page_size=10",
        status_code=429,
        json={"code": 402901, "message": "Monthly quota exceeded."},
    )

    client = SoSoValueClient()
    with pytest.raises(SoSoValueMonthlyQuotaError):
        await client.get_macro_events()


async def test_rate_limit_retries_once_then_succeeds(httpx_mock, monkeypatch):
    monkeypatch.setattr(settings, "sosovalue_use_fixtures", False)
    monkeypatch.setattr(settings, "sosovalue_api_key", "SOSO-test")
    # Make backoff near-instant so the test isn't slow
    monkeypatch.setattr("etfpulse.adapters.sosovalue._RATE_LIMIT_BACKOFF_SECONDS", 0.01)

    url = f"{settings.sosovalue_base_url}/macro/events?page=1&page_size=10"
    # First call: rate-limited
    httpx_mock.add_response(
        url=url,
        status_code=429,
        json={"code": 402901, "message": "Too many requests. Rate limit exceeded."},
    )
    # Second call: success
    httpx_mock.add_response(
        url=url,
        status_code=200,
        json={"code": 0, "message": "success", "data": []},
    )

    client = SoSoValueClient()
    result = await client.get_macro_events()
    assert result == []


async def test_server_error_wraps_in_sosovalue_error(httpx_mock, monkeypatch):
    """5xx from upstream must raise `SoSoValueError` — not bare `httpx.HTTPStatusError`."""
    monkeypatch.setattr(settings, "sosovalue_use_fixtures", False)
    monkeypatch.setattr(settings, "sosovalue_api_key", "SOSO-test")

    httpx_mock.add_response(
        url=f"{settings.sosovalue_base_url}/macro/events?page=1&page_size=10",
        status_code=500,
        text="<html>Bad Gateway</html>",
    )

    client = SoSoValueClient()
    with pytest.raises(SoSoValueError, match="HTTP 500"):
        await client.get_macro_events()


async def test_non_json_response_wraps_in_sosovalue_error(httpx_mock, monkeypatch):
    """A 200 with non-JSON body (e.g. Cloudflare error HTML) must wrap the decode failure."""
    monkeypatch.setattr(settings, "sosovalue_use_fixtures", False)
    monkeypatch.setattr(settings, "sosovalue_api_key", "SOSO-test")

    httpx_mock.add_response(
        url=f"{settings.sosovalue_base_url}/macro/events?page=1&page_size=10",
        status_code=200,
        text="<html>not json</html>",
    )

    client = SoSoValueClient()
    with pytest.raises(SoSoValueError, match="invalid JSON"):
        await client.get_macro_events()
