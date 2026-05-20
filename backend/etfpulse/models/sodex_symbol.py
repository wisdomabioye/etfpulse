"""SoDEX market symbol cache (PR D.3).

Each order signed against the SoDEX gateway carries a numeric `symbol_id`
(NOT the human ticker like `vBTC_vUSDC`). Per-order resolution would mean
a `GET /markets/symbols` HTTP round-trip on every prepare_order; this
table caches the listings instead, refreshed daily.

Daily refresh + bootstrap-on-empty: the scheduler runs
`pipeline.symbols_refresh.refresh_sodex_symbols` at
`settings.symbols_refresh_cron_*` (default 04:00 UTC), and the
lifespan startup task seeds the table if it's empty so a freshly-
deployed instance can serve orders immediately. Operator can also
force a refresh via `POST /api/admin/sodex/symbols/refresh`.

Edge case (known): if SoDEX lists a new asset between daily refreshes,
the cache is stale for up to ~24h. `resolve_symbol_id` raises
`SymbolNotResolved` in that window so prepare_order surfaces a 503
with an operator-actionable message rather than silently using a wrong
symbol_id. BTC/ETH only today; flagged as a scale follow-up.
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Index,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from etfpulse.models.base import Base


class SodexSymbol(Base):
    __tablename__ = "sodex_symbols"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # Wire-format venue label — same values as `Order.venue` /
    # `Position.venue`. CHECK constraint mirrors theirs.
    venue: Mapped[str] = mapped_column(String(20), nullable=False)
    # Venue's numeric symbol ID — the int the gateway signs against.
    # Not unique across venues (spot's vBTC_vUSDC may collide with a
    # perps symbol ID); the (venue, name) UNIQUE handles uniqueness.
    symbol_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Human ticker, e.g. `vBTC_vUSDC`. Wire-verbatim from
    # `GET /markets/symbols`.
    name: Mapped[str] = mapped_column(String(40), nullable=False)
    # Normalised asset code extracted from `name` — e.g. `vBTC_vUSDC`
    # → `BTC`. Used by `resolve_symbol_id(venue, asset)` so callers
    # can ask in human terms. Extraction lives in `symbols_refresh.py`
    # so the parsing rule has one home.
    asset: Mapped[str] = mapped_column(String(10), nullable=False)
    # Full symbol row from the venue, verbatim. Held for debug + future
    # field reads (tick size, lot size, etc) without re-fetching. JSONB
    # is correct here — we never re-hash this column, only read fields.
    raw: Mapped[dict] = mapped_column(JSONB, nullable=False)
    refreshed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "venue IN ('sodex_spot','sodex_perps')",
            name="ck_sodex_symbols_venue_enum",
        ),
        # (venue, name) UNIQUE — one row per listing per venue. Refresh
        # upserts on conflict.
        UniqueConstraint("venue", "name", name="uq_sodex_symbols_venue_name"),
        # Resolve path: `WHERE venue=? AND asset=?` → ix_sodex_symbols_venue_asset.
        Index("ix_sodex_symbols_venue_asset", "venue", "asset"),
    )

    def __repr__(self) -> str:
        return (
            f"<SodexSymbol venue={self.venue} name={self.name} "
            f"id={self.symbol_id} asset={self.asset}>"
        )
