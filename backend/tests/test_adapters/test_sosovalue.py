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
    """BTC fixture has 28 rows spanning 21 unique dates (multiple rolling-aggregate
    rows per date — adapter keeps the first row per date)."""
    flows = await sosovalue_client.get_etf_flows("BTC")

    dates = [f.date for f in flows]
    assert len(dates) == len(set(dates)), "adapter must dedupe by date"
    assert len(flows) == 21

    first = flows[0]
    assert first.date == date(2026, 5, 8)
    # The first row for 2026-05-08 in the fixture has total_net_inflow=-145651012.3
    assert float(first.total_net_inflow) == pytest.approx(-145651012.3, rel=1e-9)


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
    assert len(events) == 5
    assert events[0].date == date(2026, 5, 10)
    assert events[0].events == ["Existing Home Sales"]


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


async def test_429_with_unknown_message_routes_to_retry_path(httpx_mock, monkeypatch):
    """Safe-by-construction: if SoSoValue ever changes message wording for
    EITHER 429 path (per-minute or monthly), substring-match defaults to
    the retry path. Worst case is one wasted retry on a real monthly
    event — adapter never crashes on wording drift.

    Live probe (scripts/dev/probe_rate_limit_429.py) couldn't trip per-minute
    on the upgraded tier with 35 parallel calls/1.88s — this test pins
    the routing contract so wording drift is detected in CI rather than
    in production.
    """
    monkeypatch.setattr(settings, "sosovalue_use_fixtures", False)
    monkeypatch.setattr(settings, "sosovalue_api_key", "SOSO-test")
    monkeypatch.setattr("etfpulse.adapters.sosovalue._RATE_LIMIT_BACKOFF_SECONDS", 0.01)

    url = f"{settings.sosovalue_base_url}/macro/events?page=1&page_size=10"
    # 1st attempt: 429 with NOVEL wording (not "Monthly quota", not the
    # historical per-minute string either — pretend SoSoValue rephrased it).
    httpx_mock.add_response(
        url=url,
        status_code=429,
        json={"code": 402901, "message": "request quota exhausted for this window"},
    )
    httpx_mock.add_response(
        url=url,
        status_code=200,
        json={"code": 0, "message": "success", "data": []},
    )

    client = SoSoValueClient()
    # Must NOT raise SoSoValueMonthlyQuotaError; must retry; must succeed.
    result = await client.get_macro_events()
    assert result == []


async def test_monthly_quota_wording_variant_still_routes_correctly(httpx_mock, monkeypatch):
    """Substring match: any message *containing* 'Monthly quota' (regardless
    of trailing punctuation, suffix, casing in the surrounding text) routes
    to MonthlyQuotaError. Pins the contract that wording with the same
    substring is treated identically.
    """
    monkeypatch.setattr(settings, "sosovalue_use_fixtures", False)
    monkeypatch.setattr(settings, "sosovalue_api_key", "SOSO-test")

    httpx_mock.add_response(
        url=f"{settings.sosovalue_base_url}/macro/events?page=1&page_size=10",
        status_code=429,
        # Variant wording — adds context but keeps the substring.
        json={
            "code": 402901,
            "message": "Monthly quota for this account exhausted; renews 2026-06-01",
        },
    )

    client = SoSoValueClient()
    with pytest.raises(SoSoValueMonthlyQuotaError, match="Monthly quota"):
        await client.get_macro_events()


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


# ---- Spot price + klines (issue #34) --------------------------------------


async def test_spot_price_btc_parses_from_fixture():
    """market-snapshot fixture mirrors the inferred `{data: {price: ...}}` envelope."""
    from decimal import Decimal

    client = SoSoValueClient()
    price = await client.get_spot_price("BTC")
    assert isinstance(price, Decimal)
    # Fixture value
    assert price == Decimal("82352.65")


async def test_spot_price_eth_parses_from_fixture():
    from decimal import Decimal

    client = SoSoValueClient()
    price = await client.get_spot_price("ETH")
    assert price == Decimal("2377.73")


async def test_spot_price_raises_on_missing_data_envelope(monkeypatch):
    """If the inferred `{data: {...}}` shape is wrong (e.g. data is null or missing),
    the adapter must raise rather than persist a wrong/missing price silently."""

    def _broken_fixture(name: str):
        return {"code": 0, "message": "ok", "data": None, "details": None}

    monkeypatch.setattr(SoSoValueClient, "_load_fixture", staticmethod(_broken_fixture))
    client = SoSoValueClient()
    with pytest.raises(SoSoValueError, match="unexpected response envelope"):
        await client.get_spot_price("BTC")


async def test_klines_parses_from_fixture():
    """klines fixture mirrors the inferred `{data: [{...}, ...]}` envelope."""
    from decimal import Decimal

    client = SoSoValueClient()
    # Fixture-mode ignores `limit` and returns the full captured series (90 bars).
    # The `limit` param only shapes the upstream HTTP call.
    bars = await client.get_daily_klines("BTC", limit=3)
    assert len(bars) == 90
    assert bars[-1].close == Decimal("82220.14")
    # bar_date is derived from the `timestamp` ms field — confirm it works
    assert str(bars[-1].bar_date) == "2026-05-10"


async def test_klines_raises_on_wrong_shape(monkeypatch):
    """If data comes back as a dict instead of a list (schema drift), raise."""

    def _broken_fixture(name: str):
        return {"code": 0, "message": "ok", "data": {"not": "a list"}, "details": None}

    monkeypatch.setattr(SoSoValueClient, "_load_fixture", staticmethod(_broken_fixture))
    client = SoSoValueClient()
    with pytest.raises(SoSoValueError, match="unexpected response envelope"):
        await client.get_daily_klines("BTC")


async def test_sector_spotlight_parses_fixture():
    """Real-shape fixture: 16 sector + 22 spotlight rows, BTC at 0.5944 dom."""
    from decimal import Decimal

    client = SoSoValueClient()
    s = await client.get_sector_spotlight()
    assert len(s.sector) == 16
    assert len(s.spotlight) == 22

    # find_sector resolves both BTC and ETH names — verified in the live probe.
    btc = s.find_sector("BTC")
    assert btc is not None
    assert btc.marketcap_dom == Decimal("0.5944")
    assert btc.change_pct_24h == Decimal("0.0168")

    # Defensive lookup returns None on absent name.
    assert s.find_sector("NotARealSector") is None

    # Leading-whitespace name is stripped on ingestion (see SectorRow validator).
    assert any(row.name == "PerpDEX" for row in s.spotlight)
    assert all(row.name == row.name.strip() for row in s.spotlight)


async def test_sector_spotlight_raises_on_wrong_envelope(monkeypatch):
    """If `data` comes back as a list instead of a dict (schema drift), raise."""

    def _broken_fixture(name: str):
        return {"code": 0, "message": "ok", "data": ["not", "a", "dict"], "details": None}

    monkeypatch.setattr(SoSoValueClient, "_load_fixture", staticmethod(_broken_fixture))
    client = SoSoValueClient()
    with pytest.raises(SoSoValueError, match="unexpected response envelope"):
        await client.get_sector_spotlight()


async def test_sector_spotlight_is_cached():
    """Second call should not re-load the fixture."""
    from unittest.mock import patch

    client = SoSoValueClient()
    await client.get_sector_spotlight()
    with patch.object(SoSoValueClient, "_load_fixture") as load:
        await client.get_sector_spotlight()
        load.assert_not_called()
