"""Daily refresh of the `sodex_symbols` cache from venue `/markets/symbols`.

Runs via APScheduler cron at `symbols_refresh_cron_*` (default 04:00 UTC,
30 minutes before the daily signal cycle so symbol_id resolution is
fresh by the time signal-driven orders need it). Also called once at
lifespan startup if the cache is empty (bootstrap-on-empty).

The upsert is keyed by `(venue, name)` — the table's UNIQUE — so a new
listing INSERTs, an existing listing UPDATEs `symbol_id` (if the venue
ever re-assigns) and `refreshed_at`. We never DELETE rows: a delisted
symbol would still be queryable for historical position attribution,
and stale rows don't break anything (`resolve_symbol_id` orders by
`refreshed_at DESC` so the freshest entry wins).

Asset extraction: `vBTC_vUSDC` → `BTC`. Strip leading `v`/`V` from the
base segment (before the first `_`). Same rule as
`pipeline.execution.positions_perps._normalise_venue_position` —
kept in lock-step so cache keys and reconcile keys match.

D14: no commit — caller wraps the transaction.
"""

from __future__ import annotations

import structlog
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from etfpulse.adapters.sodex.perps_client import SodexPerpsClient
from etfpulse.adapters.sodex.spot_client import SodexSpotClient
from etfpulse.models.order import Venue
from etfpulse.models.sodex_symbol import SodexSymbol

log = structlog.get_logger(__name__)


def extract_asset_from_symbol_name(name: str) -> str:
    """Extract the base asset from a SoDEX symbol name.

    Handles BOTH venue naming conventions, since the value is used as a
    single canonical asset code across the system (detectors, the price
    oracle, composite scoring all key on the bare base — `BTC`, never
    `BTC-USD`):

      - **Spot** symbols are `v<BASE>_v<QUOTE>` (`vBTC_vUSDC` → `BTC`):
        split on the first `_`, strip a leading `v`/`V`.
      - **Perps** symbols are `<BASE>-<QUOTE>` (`BTC-USD` → `BTC`):
        split on the first `-`, take the base.

    The two splits compose so either shape resolves to the base:
    `vBTC_vUSDC` → `vBTC` → `vBTC` → `BTC`; `BTC-USD` → `BTC-USD` →
    `BTC` → `BTC`. (A perps base never contains an internal `-`, and a
    spot base never contains a `-`, so taking the first segment is
    correct for both.)

    Same rule applied across the codebase so cache resolution and
    reconcile-side asset comparisons agree on what `asset='BTC'` means
    (`pipeline.execution.positions_perps._normalise_venue_position`
    calls this directly — kept in lock-step here, not by copying).

    Raises ValueError on empty input OR fully-empty output. The
    fully-empty case triggers when both the stripped result AND the raw
    base segment are empty — e.g. `"_USDC"` or `"-USD"` (no base before
    the separator). Inputs like `"v_USDC"` (base is just `"v"`) DO NOT
    raise; the fallback returns `"v"`. Today's venue symbols never
    produce a pure-`v` base, so the fallback never fires in practice.
    """
    if not name:
        raise ValueError("extract_asset_from_symbol_name: empty name")
    # Spot quote is `_`-delimited (vBTC_vUSDC); perps quote is `-`-delimited
    # (BTC-USD). Take the base segment ahead of whichever appears first.
    base = name.split("_", 1)[0].split("-", 1)[0]
    result = base.lstrip("vV")
    # If after stripping we're empty, fall back to the raw base (e.g. a
    # purely-`v` segment) — but if THAT is empty too, raise.
    if not result:
        result = base
    if not result:
        raise ValueError(
            f"extract_asset_from_symbol_name: malformed name {name!r} "
            "yields empty asset after parsing — venue may have produced "
            "an unexpected symbol shape"
        )
    return result


async def refresh_sodex_symbols(
    session: AsyncSession,
    *,
    spot_client: SodexSpotClient,
    perps_client: SodexPerpsClient,
) -> dict[str, int]:
    """Refresh `sodex_symbols` for both venues.

    Per-venue HTTP isolation: a perps fetch failure does NOT block the
    spot refresh. Each venue is tried in its own try/except so a
    transient gateway outage on one venue doesn't poison the other.

    Returns counts: `{spot_inserted, spot_updated, perps_inserted,
    perps_updated, errors}`. `errors` is the number of venues that
    raised (0..2).
    """
    summary = {
        "spot_inserted": 0,
        "spot_updated": 0,
        "perps_inserted": 0,
        "perps_updated": 0,
        "errors": 0,
        # PR D.3.2 — per-row parse-error counters. `extract_asset_from_symbol_name`
        # now raises on malformed venue symbols (D.3.1 hardening). One bad row
        # must NOT abort the whole-venue batch; counted here + logged per row.
        "spot_parse_errors": 0,
        "perps_parse_errors": 0,
    }

    try:
        spot_symbols = await spot_client.get_symbols()
    except Exception as exc:  # noqa: BLE001 — bounded scope
        log.warning("symbols_refresh_spot_failed", error=str(exc))
        summary["errors"] += 1
    else:
        for spot_sym in spot_symbols:
            # PR D.3.3 — each per-row upsert runs inside a SAVEPOINT
            # (`session.begin_nested`). If `_upsert_symbol` raises a
            # DB-level error (network blip, deadlock, FK violation),
            # the savepoint rolls back and the outer session stays
            # usable for subsequent rows. Without the SAVEPOINT, the
            # session enters "failed-tx" state on first error and
            # every subsequent `session.execute` raises — defeating
            # the per-row resilience the try/except is supposed to
            # provide. Mirrors the pattern in
            # `pipeline.execution.pipeline._insert_order_with_clordid_retry`.
            inserted: bool | None = None
            try:
                async with session.begin_nested():
                    asset = extract_asset_from_symbol_name(spot_sym.name)
                    inserted = await _upsert_symbol(
                        session,
                        venue=Venue.SODEX_SPOT.value,
                        symbol_id=spot_sym.id,
                        name=spot_sym.name,
                        asset=asset,
                        raw=spot_sym.model_dump(by_alias=True),
                    )
            except Exception as exc:  # noqa: BLE001
                # Savepoint already rolled back when the async-with
                # block exited with an exception. Log + count + continue.
                log.warning(
                    "symbols_refresh_spot_row_failed",
                    name=spot_sym.name,
                    error=str(exc),
                )
                summary["spot_parse_errors"] += 1
                continue
            if inserted:
                summary["spot_inserted"] += 1
            else:
                summary["spot_updated"] += 1

    try:
        perps_symbols = await perps_client.get_symbols()
    except Exception as exc:  # noqa: BLE001
        log.warning("symbols_refresh_perps_failed", error=str(exc))
        summary["errors"] += 1
    else:
        for perps_sym in perps_symbols:
            # PR D.3.3 — SAVEPOINT per row; see spot loop above for rationale.
            inserted_perps: bool | None = None
            try:
                async with session.begin_nested():
                    asset = extract_asset_from_symbol_name(perps_sym.name)
                    inserted_perps = await _upsert_symbol(
                        session,
                        venue=Venue.SODEX_PERPS.value,
                        symbol_id=perps_sym.id,
                        name=perps_sym.name,
                        asset=asset,
                        raw=perps_sym.model_dump(by_alias=True),
                    )
            except Exception as exc:  # noqa: BLE001 — see spot loop above
                log.warning(
                    "symbols_refresh_perps_row_failed",
                    name=perps_sym.name,
                    error=str(exc),
                )
                summary["perps_parse_errors"] += 1
                continue
            if inserted_perps:
                summary["perps_inserted"] += 1
            else:
                summary["perps_updated"] += 1

    log.info("symbols_refresh_complete", **summary)
    return summary


async def _upsert_symbol(
    session: AsyncSession,
    *,
    venue: str,
    symbol_id: int,
    name: str,
    asset: str,
    raw: dict,
) -> bool:
    """Upsert a single sodex_symbols row keyed on `(venue, name)`.

    Returns True if a new row was inserted, False on update of an
    existing row.

    PR D.3.1: race-safe via Postgres `INSERT ... ON CONFLICT (venue, name)
    DO UPDATE`. The original D.3 implementation used SELECT-then-INSERT/
    UPDATE, which raced when admin refresh + cron fired concurrently
    (both probe-missed, both inserted, second one IntegrityError aborts
    the entire transaction). Single-statement upsert collapses the
    race to atomic.

    The `xmax` system column trick distinguishes insert (xmax=0) from
    update (xmax!=0) in a single statement without an extra round-trip.
    """
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    stmt = (
        pg_insert(SodexSymbol)
        .values(
            venue=venue,
            symbol_id=symbol_id,
            name=name,
            asset=asset,
            raw=raw,
            refreshed_at=now,
        )
        .on_conflict_do_update(
            constraint="uq_sodex_symbols_venue_name",
            set_={
                "symbol_id": symbol_id,
                "asset": asset,
                "raw": raw,
                "refreshed_at": now,
            },
        )
        # MVCC `xmax = 0` discriminator — well-known Postgres ON CONFLICT
        # idiom. Fresh INSERT: the row's `xmax` is 0 (no concurrent
        # transaction has locked it). UPDATE-on-conflict: the row's
        # `xmax` carries the updating transaction's xid (non-zero).
        # Letting us distinguish "first insert" from "upsert update" in
        # the SAME statement without a round-trip SELECT.
        #
        # DO NOT replace this with a separate SELECT-then-INSERT — that
        # re-introduces the D.3-era race (PR D.3.1 was the fix). The
        # idiom is stable across all currently-supported Postgres
        # versions; reference: https://www.postgresql.org/docs/current/
        # ecpg-pgtypes.html (system columns).
        .returning(text("(xmax = 0) AS inserted"))
    )
    result = await session.execute(stmt)
    return bool(result.scalar_one())
