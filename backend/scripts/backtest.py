"""Operator CLI — replay detectors over a historical window with candidate
threshold configs and report hit-rate per detector.

Logic in `etfpulse.pipeline.backtest` so it's unit-testable without a CLI
fixture. This file is the entry point. READ-ONLY: never writes to production
tables. The session opens, the orchestrator runs, the session ROLLS BACK on
exit (the orchestrator performs reads only, so rollback is a no-op in
practice — the explicit rollback is a belt-and-braces guard against future
accidents).

Run from backend/:

    # Defaults — production detector thresholds, no live AI, JSON to stdout.
    uv run python scripts/backtest.py --start 2026-04-15 --end 2026-05-12

    # Threshold sweep — override detector kwargs via inline JSON. The keys
    # must match the detector constructor parameters (see
    # `pipeline/detectors/__init__.py`). Unknown detector or unknown kwarg
    # raises a `TypeError` / `ValueError` at run time.
    uv run python scripts/backtest.py \\
      --start 2026-04-15 --end 2026-05-12 \\
      --config-override '{"magnitude": {"percentile_threshold": 0.85}}'

    # Persist the full structured report to disk for diffing across runs.
    uv run python scripts/backtest.py --start 2026-04-15 --end 2026-05-12 \\
      --json-out /tmp/backtest.json

    # Default is offline (cache + existing-Signal lookup only). Hits with no
    # cached / existing-signal AI answer are reported with
    # skip_reason=no_direction. To opt in to live OpenRouter calls on cache
    # miss, pass --allow-ai (currently a future seam — see flag help below).

The resolver chain (`cache → existing Signal → optional live AI`) is wired in
`pipeline.backtest.make_resolver`. This script does not import OpenRouter
directly — `--allow-ai` is plumbed for symmetry but the live caller is None
unless an operator wires one explicitly into `make_resolver`. As of PR I.5
the flag is therefore a no-op; kept here so a future PR can land the live
caller without re-shaping the CLI surface.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

from etfpulse.db import async_session
from etfpulse.pipeline.backtest import BacktestReport, make_resolver, run_backtest


def _parse_date(s: str) -> date:
    try:
        return date.fromisoformat(s)
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"invalid date {s!r}: {e}") from e


def _parse_config(raw: str | None) -> dict[str, dict[str, Any]]:
    if raw is None:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        raise argparse.ArgumentTypeError(f"--config-override is not valid JSON: {e}") from e
    if not isinstance(parsed, dict):
        raise argparse.ArgumentTypeError("--config-override must be a JSON object")
    for k, v in parsed.items():
        if not isinstance(v, dict):
            raise argparse.ArgumentTypeError(
                f"--config-override[{k!r}] must be an object of kwargs"
            )
    return parsed


def _format_summary_table(report: BacktestReport) -> str:
    """Operator-facing console table — same audience as `backfill_*` scripts.

    Two sections: top counters, then per-detector hit rate. Compact enough
    to read at a glance; the JSON output carries the full per-row detail.
    """
    lines: list[str] = []
    lines.append(f"Backtest window: {report.start} → {report.end}")
    lines.append(f"AI prompt version (cohort): {report.ai_prompt_version}")
    lines.append("")
    lines.append("Counters:")
    width = max(len(k) for k in report.counters)
    for k, v in report.counters.items():
        lines.append(f"  {k:<{width}}  {v}")
    lines.append("")
    lines.append("Per detector:")
    lines.append(
        f"  {'detector':<14} {'hits':>6} {'scored':>7} {'wins':>5} {'losses':>7} {'hit_rate':>9}"
    )
    for row in report.per_detector:
        hr = "—" if row.hit_rate is None else f"{row.hit_rate * 100:.1f}%"
        lines.append(
            f"  {row.detector_name:<14} {row.n_hits:>6} {row.n_scored:>7}"
            f" {row.wins:>5} {row.losses:>7} {hr:>9}"
        )
    return "\n".join(lines)


async def _run(args: argparse.Namespace) -> int:
    """Open a session, run the backtest, ROLL BACK, emit output. Return code
    is 0 on a clean run, 1 on an orchestrator-level error. Per-detector
    `detector_errors` are non-fatal and visible in the counters section."""
    overrides = _parse_config(args.config_override)

    async with async_session() as session:
        resolver = make_resolver(session, allow_live_ai=args.allow_ai)
        try:
            report = await run_backtest(
                session,
                start=args.start,
                end=args.end,
                detector_overrides=overrides,
                ai_resolver=resolver,
            )
        finally:
            # Explicit rollback — the orchestrator does only reads, but a
            # future change that accidentally adds an `session.add()` would
            # otherwise leak into the connection's pending state on commit.
            await session.rollback()

    print(_format_summary_table(report))
    if args.json_out:
        out_path = Path(args.json_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report.to_json_dict(), indent=2), encoding="utf-8")
        print(f"\nReport JSON: {out_path}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--start",
        type=_parse_date,
        required=True,
        help="ISO date — first day of the backtest window (inclusive).",
    )
    parser.add_argument(
        "--end",
        type=_parse_date,
        required=True,
        help="ISO date — last day of the backtest window (inclusive).",
    )
    parser.add_argument(
        "--config-override",
        type=str,
        default=None,
        help=(
            "JSON object mapping detector name to constructor-kwargs to "
            'override. Example: \'{"magnitude": {"percentile_threshold": 0.85}}\'. '
            "Unspecified detectors run with prod defaults from settings."
        ),
    )
    parser.add_argument(
        "--json-out",
        type=str,
        default=None,
        help="Path to write the full structured report as JSON.",
    )
    parser.add_argument(
        "--allow-ai",
        action="store_true",
        help=(
            "Opt-in to live OpenRouter calls when the resolver chain misses "
            "(cache + existing-signal lookup). NOTE: as of PR I.5 this flag "
            "is a no-op — the CLI does not yet wire a live AI caller into "
            "make_resolver. Sweeps run offline regardless of this flag. "
            "Kept as a future seam."
        ),
    )
    args = parser.parse_args()
    if args.end < args.start:
        parser.error(f"--end ({args.end}) must be >= --start ({args.start})")
    sys.exit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
