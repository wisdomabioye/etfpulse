"""One-shot live verification of SoSoValue endpoints.

Hits each endpoint we depend on exactly once with a small page size so we
spend ~6 calls of monthly quota. Dumps raw JSON + DTO comparison so any
shape drift since the spike is obvious.

Run: SOSOVALUE_USE_FIXTURES=false uv run python scripts/verify_sosovalue.py
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import httpx

# Force live mode regardless of .env
os.environ["SOSOVALUE_USE_FIXTURES"] = "false"

from etfpulse.adapters.sosovalue import (  # noqa: E402
    BTC_CURRENCY_ID,
    SoSoValueError,
    sosovalue_client,
)
from etfpulse.config import settings  # noqa: E402
from etfpulse.models import NewsCategory  # noqa: E402


def _trunc(obj: Any, depth: int = 2) -> Any:
    """Pretty-print first-N keys, truncate long values."""
    if isinstance(obj, dict):
        return {k: _trunc(v, depth - 1) if depth > 0 else "..." for k, v in obj.items()}
    if isinstance(obj, list):
        return [_trunc(x, depth - 1) for x in obj[:2]] + (["..."] if len(obj) > 2 else [])
    if isinstance(obj, str) and len(obj) > 120:
        return obj[:120] + "..."
    return obj


async def raw_get(path: str, **params: Any) -> tuple[int, Any]:
    """Bypass adapter — direct httpx for endpoints not yet wired."""
    url = f"{settings.sosovalue_base_url}{path}"
    headers = {"x-soso-api-key": settings.sosovalue_api_key, "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.get(url, headers=headers, params=params)
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, {"_raw_text": r.text[:500]}


async def main() -> None:
    print(f"=== SoSoValue live verification ===")
    print(f"base_url: {settings.sosovalue_base_url}")
    print(f"key set: {bool(settings.sosovalue_api_key)}")
    print(f"use_fixtures: {settings.sosovalue_use_fixtures}\n")

    # 1. ETF flows
    print("--- 1. get_etf_flows(BTC, limit=3) ---")
    try:
        flows = await sosovalue_client.get_etf_flows("BTC", limit=3)
        print(f"  parsed OK, {len(flows)} rows")
        if flows:
            print(f"  first row: {flows[0].model_dump()}")
    except SoSoValueError as e:
        print(f"  FAILED: {type(e).__name__}: {e}")

    # 2. News (institution category)
    print("\n--- 2. get_news(INSTITUTION, page_size=3) ---")
    try:
        news = await sosovalue_client.get_news(
            category=NewsCategory.INSTITUTION, page_size=3
        )
        print(f"  parsed OK, {len(news)} articles")
        if news:
            n = news[0]
            print(f"  first: id={n.id} title={n.display_title[:80]!r}")
            print(f"         currencies={n.matched_currencies}")
    except SoSoValueError as e:
        print(f"  FAILED: {type(e).__name__}: {e}")

    # 3. Macro events
    print("\n--- 3. get_macro_events(page_size=3) ---")
    try:
        events = await sosovalue_client.get_macro_events(page_size=3)
        print(f"  parsed OK, {len(events)} events")
        if events:
            print(f"  first: {events[0].model_dump()}")
    except SoSoValueError as e:
        print(f"  FAILED: {type(e).__name__}: {e}")

    # 4. Spot price
    print("\n--- 4. get_spot_price(BTC) ---")
    try:
        price = await sosovalue_client.get_spot_price("BTC")
        print(f"  parsed OK, price={price}")
    except SoSoValueError as e:
        print(f"  FAILED: {type(e).__name__}: {e}")

    # 5. Daily klines
    print("\n--- 5. get_daily_klines(BTC, limit=3) ---")
    try:
        bars = await sosovalue_client.get_daily_klines("BTC", limit=3)
        print(f"  parsed OK, {len(bars)} bars")
        if bars:
            print(f"  first: {bars[0].model_dump()}")
    except SoSoValueError as e:
        print(f"  FAILED: {type(e).__name__}: {e}")

    # 6. Sector spotlight — RAW probe, not yet wired
    print("\n--- 6. /currencies/sector-spotlight (RAW probe) ---")
    # Try a couple of plausible parameterizations
    for params in ({"currency_id": BTC_CURRENCY_ID}, {"symbol": "BTC"}, {}):
        status, body = await raw_get("/currencies/sector-spotlight", **params)
        print(f"  params={params} -> HTTP {status}")
        print(f"  body: {json.dumps(_trunc(body), indent=2, default=str)[:1500]}")
        if status == 200:
            break

    print("\n=== done ===")


if __name__ == "__main__":
    asyncio.run(main())
