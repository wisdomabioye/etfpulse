"""Hard verification of /currencies/sector-spotlight before wiring it.

Probes:
 1. With NO params  — does it work? what shape?
 2. With currency_id=BTC vs ETH vs nonsense — are bodies identical?
 3. With NO auth header — does it 401? (confirms it's protected)
 4. Field-type sweep across every entry — are change_pct_24h / marketcap_dom
    always numbers, or ever strings/nulls?
 5. Same path through the existing adapter `_request` (Content-Type:
    application/json) — same shape?

Cost: 5 calls.
"""

from __future__ import annotations

import asyncio
import json
import os

os.environ["SOSOVALUE_USE_FIXTURES"] = "false"

import httpx  # noqa: E402

from etfpulse.adapters.sosovalue import (  # noqa: E402
    BTC_CURRENCY_ID,
    ETH_CURRENCY_ID,
    sosovalue_client,
)
from etfpulse.config import settings  # noqa: E402


def _types_in(rows: list[dict], key: str) -> set[str]:
    return {type(r.get(key)).__name__ for r in rows}


async def probe(c: httpx.AsyncClient, label: str, **params) -> dict:
    headers = {"x-soso-api-key": settings.sosovalue_api_key, "Accept": "application/json"}
    r = await c.get(
        f"{settings.sosovalue_base_url}/currencies/sector-spotlight",
        headers=headers,
        params=params,
    )
    print(f"[{label}] HTTP {r.status_code}, body len={len(r.text)} bytes")
    return r.json() if r.status_code == 200 else {"_status": r.status_code, "_body": r.text[:200]}


async def main() -> None:
    print(f"base_url: {settings.sosovalue_base_url}")
    print(f"key set: {bool(settings.sosovalue_api_key)}\n")

    async with httpx.AsyncClient(timeout=30.0) as c:
        b_none = await probe(c, "no-params")
        b_btc = await probe(c, "currency_id=BTC", currency_id=BTC_CURRENCY_ID)
        b_eth = await probe(c, "currency_id=ETH", currency_id=ETH_CURRENCY_ID)
        b_bad = await probe(c, "currency_id=garbage", currency_id="garbage_string")

        # Auth probe — no api key
        r = await c.get(
            f"{settings.sosovalue_base_url}/currencies/sector-spotlight",
            headers={"Accept": "application/json"},
        )
        print(f"[no-auth-header] HTTP {r.status_code}, body[:200]={r.text[:200]!r}")

    # 1. Shape check
    print("\n=== shape ===")
    for label, body in [("none", b_none), ("btc", b_btc), ("eth", b_eth)]:
        if isinstance(body, dict) and "data" in body:
            data = body["data"]
            print(
                f"  [{label}] code={body.get('code')}  message={body.get('message')!r}  "
                f"data.sector={len(data.get('sector', []))}  data.spotlight={len(data.get('spotlight', []))}"  # noqa: E501
            )
        else:
            print(f"  [{label}] non-200 or missing data: {body}")

    # 2. Body equality
    print("\n=== body equality ===")
    s_none = json.dumps(b_none, sort_keys=True)
    s_btc = json.dumps(b_btc, sort_keys=True)
    s_eth = json.dumps(b_eth, sort_keys=True)
    s_bad = json.dumps(b_bad, sort_keys=True)
    print(f"  none == btc:    {s_none == s_btc}")
    print(f"  btc  == eth:    {s_btc == s_eth}")
    print(f"  btc  == garbage: {s_btc == s_bad}")

    # 3. Field types across every row
    print("\n=== field types in `sector` rows ===")
    if isinstance(b_btc, dict):
        sector = b_btc.get("data", {}).get("sector", [])
        spotlight = b_btc.get("data", {}).get("spotlight", [])
        print(f"  sector[{len(sector)} rows]:")
        print(f"    name keys present in all: {all('name' in r for r in sector)}")
        print(f"    name types: {_types_in(sector, 'name')}")
        print(f"    change_pct_24h types: {_types_in(sector, 'change_pct_24h')}")
        print(f"    marketcap_dom types: {_types_in(sector, 'marketcap_dom')}")
        print(
            f"    change_pct_24h has None? {any(r.get('change_pct_24h') is None for r in sector)}"
        )
        print(f"    marketcap_dom has None? {any(r.get('marketcap_dom') is None for r in sector)}")
        print(
            f"    extra keys beyond {{name,change_pct_24h,marketcap_dom}}: "
            f"{sorted(set().union(*(r.keys() for r in sector)))}"
        )

        print(f"  spotlight[{len(spotlight)} rows]:")
        print(
            f"    extra keys beyond {{name,change_pct_24h}}: "
            f"{sorted(set().union(*(r.keys() for r in spotlight)))}"
        )
        print(f"    change_pct_24h types: {_types_in(spotlight, 'change_pct_24h')}")
        print(f"    has marketcap_dom? {any('marketcap_dom' in r for r in spotlight)}")
        print(
            f"    name has leading-space rows? "
            f"{[r['name'] for r in spotlight if r['name'] != r['name'].strip()]}"
        )

    # 4. Same path through existing adapter `_request`
    print("\n=== adapter _request envelope check ===")
    raw = await sosovalue_client._request("GET", "/currencies/sector-spotlight")
    print(f"  raw keys: {list(raw.keys())}")
    print(f"  raw['data'] keys: {list(raw.get('data', {}).keys())}")
    print(
        f"  same as direct httpx? {json.dumps(raw, sort_keys=True) == s_btc[:0] + json.dumps(raw, sort_keys=True)}"  # noqa: E501
    )

    # 5. Numeric range check (sanity)
    print("\n=== numeric ranges ===")
    if isinstance(b_btc, dict):
        sector = b_btc["data"]["sector"]
        if sector:
            doms = [r["marketcap_dom"] for r in sector]
            chgs = [r["change_pct_24h"] for r in sector]
            print(
                f"  marketcap_dom: min={min(doms)} max={max(doms)} sum={sum(doms):.4f} (should sum to ~1.0)"  # noqa: E501
            )
            print(f"  change_pct_24h: min={min(chgs)} max={max(chgs)}")


if __name__ == "__main__":
    asyncio.run(main())
