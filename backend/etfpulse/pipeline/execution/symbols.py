"""Symbol-id resolution against the `sodex_symbols` cache table.

The SoDEX gateway signs against numeric `symbol_id`s (e.g. spot's
`vBTC_vUSDC` is `id=1`). `prepare_order` accepts a human asset code
(`"BTC"`) and needs to translate to the venue's int id without
round-tripping `GET /markets/symbols` per call. The `sodex_symbols`
table is the cache; daily refresh repopulates it (D.3
`pipeline/symbols_refresh.py`), with bootstrap-on-empty at lifespan
startup.

Multi-quote disambiguation (V1 simplification): `sodex_symbols.asset`
is the BASE asset extracted from the symbol name (`vBTC_vUSDC` →
`BTC`). When multiple quote currencies exist for the same base (e.g.
`vBTC_vUSDC` + `vBTC_vUSDT` both have `asset='BTC'`), this resolver
returns the most-recently-refreshed match — which is arbitrary among
ties. SoDEX testnet today lists one quote per base, so the
ambiguity doesn't bite. When/if it does, `resolve_symbol_id` should
grow a `quote` parameter and the call sites should pass it explicitly.
Flagged in the module docstring rather than hidden in code so the
limitation is visible.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from etfpulse.models.sodex_symbol import SodexSymbol


class SymbolNotResolved(Exception):  # noqa: N818 — domain term, not a generic Error
    """No `sodex_symbols` row matches the requested `(venue, asset)`.

    Two distinct causes operationally:

      1. **Cache cold-start.** `sodex_symbols` is empty (lifespan
         bootstrap didn't run, or the venue's `/markets/symbols`
         endpoint failed). Operator action: `POST /api/admin/sodex/
         symbols/refresh` to populate.
      2. **New listing not yet refreshed.** SoDEX listed an asset
         between daily refreshes. Operator action: same admin route
         to force a refresh now.

    Routes catching this should return 503 with an operator-actionable
    message (NOT a generic 500). The error carries `venue` and `asset`
    so dashboards can pin specific gaps.
    """

    def __init__(self, *, venue: str, asset: str):
        super().__init__(f"no sodex_symbols row for venue={venue!r} asset={asset!r}")
        self.venue = venue
        self.asset = asset


async def resolve_symbol_id(session: AsyncSession, venue: str, asset: str) -> int:
    """Look up the venue's numeric `symbol_id` for `(venue, asset)`.

    Reads `sodex_symbols`, ordered by `refreshed_at DESC` so the freshest
    match wins on multi-quote ties (V1 limitation — see module docstring).

    Raises `SymbolNotResolved` if no row matches. The exception is the
    expected outcome on cache cold-start or new-listing-not-yet-cached;
    callers should handle it with a 503 surface, not a 500.
    """
    row = await session.execute(
        select(SodexSymbol.symbol_id)
        .where(SodexSymbol.venue == venue, SodexSymbol.asset == asset)
        .order_by(SodexSymbol.refreshed_at.desc())
        .limit(1)
    )
    sid = row.scalar()
    if sid is None:
        raise SymbolNotResolved(venue=venue, asset=asset)
    return int(sid)
