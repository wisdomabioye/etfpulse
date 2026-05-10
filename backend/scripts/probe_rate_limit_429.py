"""Live verification: confirm the per-minute 429 message text the SoSoValue
adapter relies on for routing (Monthly vs per-minute).

Strategy:
    Burst direct httpx calls (bypassing the adapter's TTLCache and
    `_request` retry logic) to /macro/events as fast as possible. Stop
    on the first HTTP 429 — which we expect to be per-minute.

Safety:
    - Caps at 35 attempts to prevent runaway quota burn (20/min limit
      means we trip rate-limit well before 35).
    - Stops immediately on first 429 — capturing the body without
      issuing further calls.
    - Inspects the captured body and asserts the adapter's classifier
      (`_classify_and_raise_429`) routes correctly. If the message
      wording has drifted from the historical
      `"Too many requests. Rate limit exceeded."` form, we surface
      that diff loudly so the test + classifier can be updated together.

Cost: ~21-25 calls (whatever it takes to trip per-minute).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from unittest.mock import MagicMock

os.environ["SOSOVALUE_USE_FIXTURES"] = "false"

import httpx  # noqa: E402

from etfpulse.adapters.sosovalue import SoSoValueClient, SoSoValueMonthlyQuotaError  # noqa: E402
from etfpulse.config import settings  # noqa: E402

_MAX_ATTEMPTS = 35


async def main() -> None:
    url = f"{settings.sosovalue_base_url}/macro/events"
    headers = {"x-soso-api-key": settings.sosovalue_api_key, "Accept": "application/json"}
    params = {"page": 1, "page_size": 1}

    print(f"Issuing {_MAX_ATTEMPTS} parallel calls to /macro/events...")
    start = time.monotonic()
    captured = None

    async with httpx.AsyncClient(timeout=10.0) as c:

        async def _one(i: int):
            try:
                return i, await c.get(url, headers=headers, params=params)
            except httpx.RequestError as exc:
                return i, exc

        results = await asyncio.gather(
            *(_one(i) for i in range(1, _MAX_ATTEMPTS + 1)),
            return_exceptions=False,
        )

    elapsed = time.monotonic() - start
    statuses: dict[int, int] = {}
    for i, r in results:
        if isinstance(r, Exception):
            print(f"  attempt {i}: network error: {r}")
            continue
        statuses[r.status_code] = statuses.get(r.status_code, 0) + 1
        if r.status_code == 429 and captured is None:
            captured = {
                "status_code": r.status_code,
                "body": (
                    r.json()
                    if r.headers.get("content-type", "").startswith("application/json")
                    else r.text
                ),
                "headers": dict(r.headers),
            }

    print(f"\nfinished in {elapsed:.2f}s; status counts: {statuses}")

    if captured is None:
        print("\nDID NOT TRIP per-minute 429.")
        print("Either the new tier raised the per-minute floor above 20/min,")
        print("or the requests were faster than we measured. Inspect logs / retry.")
        return

    print(f"\n=== captured 429 body ===\n{json.dumps(captured['body'], indent=2)}")
    print("\n=== rate-limit headers (if any) ===")
    for k, v in captured["headers"].items():
        if (
            "rate" in k.lower()
            or "limit" in k.lower()
            or "remaining" in k.lower()
            or "retry" in k.lower()
        ):
            print(f"  {k}: {v}")

    # Build a fake httpx.Response for the classifier — easier than
    # constructing a real one and sufficient for the substring check.
    fake = MagicMock()
    fake.json = lambda: captured["body"]

    msg = str(captured["body"].get("message", "")) if isinstance(captured["body"], dict) else ""
    print(f"\nmessage field: {msg!r}")
    print(f"contains 'Monthly quota'? {'Monthly quota' in msg}")

    # Run the actual classifier
    try:
        SoSoValueClient._classify_and_raise_429(fake, "/macro/events")
        print(
            "classifier verdict: NOT monthly-quota → adapter would retry once (correct for per-minute)"  # noqa: E501
        )
    except SoSoValueMonthlyQuotaError as exc:
        print(
            f"classifier verdict: MONTHLY QUOTA → adapter would NOT retry (would be wrong for per-minute!): {exc}"  # noqa: E501
        )
        print("\n!!! WORDING DRIFT — investigate before relying on the classifier")

    # Compare to the historically-assumed wording
    historical = "Too many requests. Rate limit exceeded."
    if msg == historical:
        print("\nmessage matches historical wording exactly. No action needed.")
    else:
        print(f"\nmessage DIFFERS from historical {historical!r}.")
        print("Classifier still routes correctly because it matches by 'Monthly quota' substring,")
        print(
            "BUT the test fixture in test_sosovalue.py uses the historical string and should be updated."  # noqa: E501
        )


if __name__ == "__main__":
    asyncio.run(main())
