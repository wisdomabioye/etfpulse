"""Capture full /currencies/sector-spotlight response → fixture."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

os.environ["SOSOVALUE_USE_FIXTURES"] = "false"

import httpx  # noqa: E402

from etfpulse.adapters.sosovalue import BTC_CURRENCY_ID, FIXTURES_DIR  # noqa: E402
from etfpulse.config import settings  # noqa: E402


async def main() -> None:
    url = f"{settings.sosovalue_base_url}/currencies/sector-spotlight"
    headers = {"x-soso-api-key": settings.sosovalue_api_key, "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.get(url, headers=headers, params={"currency_id": BTC_CURRENCY_ID})

    print(f"HTTP {r.status_code}")
    body = r.json()

    # Save fixture for future offline tests
    fixture_path = Path(FIXTURES_DIR) / "sosovalue_sector_spotlight_btc.json"
    fixture_path.write_text(json.dumps(body, indent=2))
    print(f"saved → {fixture_path}")
    print(f"\nfull body:\n{json.dumps(body, indent=2)[:4000]}")


if __name__ == "__main__":
    asyncio.run(main())
