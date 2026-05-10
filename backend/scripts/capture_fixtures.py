"""Refresh every SoSoValue fixture from the live API.

Captures the raw `{code, message, data, ...}` envelope so the adapter's
`raw.get("data", ...)` path is exercised correctly when fixtures are
loaded back. Total cost: ~9 calls.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

os.environ["SOSOVALUE_USE_FIXTURES"] = "false"

import httpx  # noqa: E402

from etfpulse.adapters.sosovalue import (  # noqa: E402
    BTC_CURRENCY_ID,
    ETH_CURRENCY_ID,
    FIXTURES_DIR,
)
from etfpulse.config import settings  # noqa: E402


async def get(client: httpx.AsyncClient, path: str, **params) -> dict:
    headers = {"x-soso-api-key": settings.sosovalue_api_key}
    r = await client.get(f"{settings.sosovalue_base_url}{path}", headers=headers, params=params)
    r.raise_for_status()
    return r.json()


async def main() -> None:
    out: list[tuple[str, dict]] = []

    async with httpx.AsyncClient(timeout=30.0) as c:
        out.append(
            (
                "sosovalue_etf_flows_btc",
                await get(c, "/etfs/summary-history", symbol="BTC", country_code="US", limit=50),
            )
        )
        out.append(
            (
                "sosovalue_etf_flows_eth",
                await get(c, "/etfs/summary-history", symbol="ETH", country_code="US", limit=50),
            )
        )
        out.append(
            (
                "sosovalue_news_institution",
                await get(c, "/news", language="en", page=1, page_size=20, category=3),
            )
        )
        out.append(
            (
                "sosovalue_news_announcement",
                await get(c, "/news", language="en", page=1, page_size=20, category=7),
            )
        )
        out.append(
            ("sosovalue_macro_events", await get(c, "/macro/events", page=1, page_size=10))
        )
        out.append(
            (
                "sosovalue_market_snapshot_btc",
                await get(c, f"/currencies/{BTC_CURRENCY_ID}/market-snapshot"),
            )
        )
        out.append(
            (
                "sosovalue_market_snapshot_eth",
                await get(c, f"/currencies/{ETH_CURRENCY_ID}/market-snapshot"),
            )
        )
        out.append(
            (
                "sosovalue_klines_btc",
                await get(c, f"/currencies/{BTC_CURRENCY_ID}/klines", interval="1d", limit=100),
            )
        )
        out.append(
            (
                "sosovalue_klines_eth",
                await get(c, f"/currencies/{ETH_CURRENCY_ID}/klines", interval="1d", limit=100),
            )
        )

    fdir = Path(FIXTURES_DIR)
    for name, body in out:
        path = fdir / f"{name}.json"
        path.write_text(json.dumps(body, indent=2))
        # quick shape header
        envelope_keys = list(body.keys())
        data = body.get("data")
        if isinstance(data, list):
            data_summary = f"data: list[{len(data)}]"
            sample_keys = list(data[0].keys()) if data else []
        elif isinstance(data, dict):
            if "list" in data and isinstance(data["list"], list):
                data_summary = f"data.list: list[{len(data['list'])}]"
                sample_keys = list(data["list"][0].keys()) if data["list"] else []
            else:
                data_summary = f"data: dict {list(data.keys())}"
                sample_keys = []
        else:
            data_summary = f"data: {type(data).__name__}"
            sample_keys = []
        print(f"  {name}: envelope={envelope_keys} {data_summary}")
        if sample_keys:
            print(f"    sample item keys: {sample_keys}")

    print(f"\nWrote {len(out)} fixtures to {fdir}")


if __name__ == "__main__":
    asyncio.run(main())
