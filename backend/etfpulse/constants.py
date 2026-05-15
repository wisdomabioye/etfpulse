"""Package-level constants shared across domain layers.

This module sits at the top of the import graph — `models/`, `adapters/`,
`pipeline/`, `bot/`, and `api/` may all import from it. It must NOT import
from any of them (would cycle).

Today it holds just one value, but the rationale for centralising it
generalises: any value that more than one domain layer needs to agree on
goes here, so a change requires editing a single place and the type
checker / tests catch drift automatically.

`SUPPORTED_ASSETS` is the asset universe ETFPulse processes — the
SoSoValue adapter pulls flows for these, the detectors iterate them,
the price adapter has Binance symbol mappings for them, the bot
validates user input against them, and the public API exposes them as
a Literal type. The Literal mirror in `pipeline/prices.py:Asset` MUST
match this tuple by content (Python's typing.Literal can't derive
literal values from a variable, so the duplication is mechanical and
guarded by a test in `tests/test_constants.py`).
"""

from __future__ import annotations

from typing import Final

SUPPORTED_ASSETS: Final[tuple[str, ...]] = ("BTC", "ETH")

# Cross-asset sentinel — used as `Signal.asset` for market-wide events that
# don't belong to any single tracked asset (e.g. regime_shift signals after
# PR F.3). Deliberately NOT a member of `SUPPORTED_ASSETS` because it is not
# part of the asset universe — flows aren't ingested for it, klines aren't
# fetched for it, and user `pref_assets` lists don't include it. Fan-out
# bypasses the `pref_assets` filter when `Signal.asset == MARKET_ASSET` so
# users still receive these signals regardless of their asset preferences;
# outcome scoring skips them because there's no asset price to score against.
MARKET_ASSET: Final[str] = "MARKET"
