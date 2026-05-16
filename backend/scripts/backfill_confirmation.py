"""One-shot CLI — populate Signal.confirmation_score for historical NULLs.

Logic in `etfpulse.pipeline.confirmation_backfill` so it can be unit-tested
without a CLI fixture. This file is the entry point; safe to re-run
(idempotent — only touches NULL rows where direction is derivable).

Run from backend/:

    uv run python scripts/backfill_confirmation.py [--limit N]

`--limit` caps the batch size; absent = unlimited drain. Re-fire until
"Candidates" prints 0 to confirm the backlog is empty.
"""

from __future__ import annotations

import argparse
import asyncio

from etfpulse.db import async_session
from etfpulse.pipeline.confirmation_backfill import backfill_null_confirmation


async def _run(limit: int | None) -> None:
    """Open a session, run the backfill, commit. D14 contract.

    A mid-run exception propagates (asyncio.run prints the traceback) and
    the session contextmanager rolls back unwritten changes. Re-run after
    fixing — the NULL-only filter means already-scored rows aren't re-touched.
    """
    async with async_session() as session:
        summary = await backfill_null_confirmation(session, limit=limit)
        await session.commit()

    if summary["candidates"] == 0:
        print("No signals with NULL confirmation_score. Nothing to do.")
        return

    print(f"Candidates (NULL rows):                  {summary['candidates']}")
    print(f"Updated:                                 {summary['updated']}")
    print(f"Skipped (no direction / wait / unknown): {summary['skipped_no_direction']}")
    print(f"Skipped (unsupported asset):             {summary['skipped_unsupported_asset']}")
    print(f"Errored (per-signal D13 catch):          {summary['errored']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap per-run batch size. Absent = unlimited drain.",
    )
    args = parser.parse_args()
    asyncio.run(_run(args.limit))


if __name__ == "__main__":
    main()
