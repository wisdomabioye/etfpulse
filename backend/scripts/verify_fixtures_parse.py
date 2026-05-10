"""Force the adapter into fixture mode and parse every fixture.

This is offline — no API calls. Confirms the refreshed fixtures still
parse cleanly through the existing DTOs. If a field shape drifts,
Pydantic raises here instead of in production.
"""

from __future__ import annotations

import asyncio
import os

os.environ["SOSOVALUE_USE_FIXTURES"] = "true"

from etfpulse.adapters.sosovalue import sosovalue_client  # noqa: E402
from etfpulse.models import NewsCategory  # noqa: E402


async def main() -> None:
    print("--- ETF flows BTC ---")
    flows = await sosovalue_client.get_etf_flows("BTC")
    print(f"  {len(flows)} parsed; head: {flows[0].model_dump() if flows else None}")

    print("--- ETF flows ETH ---")
    flows = await sosovalue_client.get_etf_flows("ETH")
    print(f"  {len(flows)} parsed; head: {flows[0].model_dump() if flows else None}")

    print("--- News INSTITUTION ---")
    news = await sosovalue_client.get_news(
        category=NewsCategory.INSTITUTION, fixture_name="sosovalue_news_institution"
    )
    print(f"  {len(news)} parsed")
    cur_count = sum(1 for n in news if n.matched_currencies)
    print(f"  {cur_count}/{len(news)} have matched_currencies populated")
    if news:
        n = news[0]
        print(f"  head: id={n.id} cat={n.category} release_time={n.release_time}")
        print(f"        released_at={n.released_at}")
        print(f"        display_title={n.display_title[:80]!r}")

    print("--- News ANNOUNCEMENT ---")
    news = await sosovalue_client.get_news(
        category=NewsCategory.ANNOUNCEMENT, fixture_name="sosovalue_news_announcement"
    )
    print(f"  {len(news)} parsed")
    cur_count = sum(1 for n in news if n.matched_currencies)
    print(f"  {cur_count}/{len(news)} have matched_currencies populated")

    print("--- Macro events ---")
    events = await sosovalue_client.get_macro_events()
    print(f"  {len(events)} parsed; head: {events[0].model_dump() if events else None}")

    print("--- Spot price BTC ---")
    p = await sosovalue_client.get_spot_price("BTC")
    print(f"  price={p}")

    print("--- Spot price ETH ---")
    p = await sosovalue_client.get_spot_price("ETH")
    print(f"  price={p}")

    print("--- Klines BTC ---")
    bars = await sosovalue_client.get_daily_klines("BTC")
    print(f"  {len(bars)} parsed; head: {bars[0].model_dump() if bars else None}")
    print(f"        bar_date(head)={bars[0].bar_date if bars else None}")

    print("--- Klines ETH ---")
    bars = await sosovalue_client.get_daily_klines("ETH")
    print(f"  {len(bars)} parsed; head: {bars[0].model_dump() if bars else None}")


if __name__ == "__main__":
    asyncio.run(main())
