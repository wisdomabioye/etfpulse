"""One-shot backfill — populate Signal.price_at_creation for historical NULLs.

Logic lives in `etfpulse.pipeline.price_backfill` so it can be unit-tested.
This file is the CLI entry point; safe to re-run (idempotent — only
touches rows where `price_at_creation IS NULL`).

Run from backend/:

    uv run python scripts/backfill_signal_prices.py
"""

from __future__ import annotations

import asyncio

from etfpulse.db import async_session
from etfpulse.pipeline.price_backfill import backfill_null_prices


async def main() -> None:
    """Open a session, run the backfill, commit. D14 contract.

    A mid-run exception will propagate (asyncio.run shows the traceback)
    and the session contextmanager will roll back unwritten changes —
    callers re-run; the NULL-only filter means already-committed updates
    aren't re-touched.
    """
    async with async_session() as session:
        summary = await backfill_null_prices(session)
        await session.commit()

    if summary["candidates"] == 0:
        print("No signals with NULL price_at_creation. Nothing to do.")
        return

    print(f"Candidates (NULL rows):                {summary['candidates']}")
    print(f"Updated:                               {summary['updated']}")
    print(f"Skipped (no matching bar):             {summary['skipped_no_bar']}")
    print(f"Skipped (both providers failed):       {summary['skipped_fetch_fail']}")
    print(f"Skipped (unsupported asset):           {summary['skipped_unsupported_asset']}")


if __name__ == "__main__":
    asyncio.run(main())
