"""Response DTO for `GET /api/prices/spot`.

Single endpoint returns both BTC and ETH spot prices so the top-nav strip
can render with one HTTP roundtrip. `source` and `fetched_at` are global
to the response (both prices share a fetch tick); the route's TTL cache
guarantees they were resolved within a single tick.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class SpotPrices(BaseModel):
    """BTC + ETH spot prices with provenance.

    `btc` / `eth` are nullable so a partial-outage tick (one provider chain
    fails for one asset) still serves the asset that succeeded instead of
    503-ing the whole strip.
    """

    btc: float | None = None
    eth: float | None = None
    source: str | None = None
    fetched_at: datetime
