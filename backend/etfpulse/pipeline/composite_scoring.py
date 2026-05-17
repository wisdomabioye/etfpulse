"""Composite-asset scoring for MARKET signals — PR I.3b.

regime_shift signals carry `asset == "MARKET"` (per PR F.3) because they
describe a cross-asset regime transition. The existing outcome evaluator
scores against single-asset entry/stop/target levels — that rubric doesn't
apply when there's no single asset.

This module supplies the alternative rubric: a weighted BTC + ETH return
over the signal's validity window, compared against the AI's directional
claim (`suggested_action`).

Public surface (all pure, no I/O):
    `weighted_composite_return(btc_pct, eth_pct, btc_weight, eth_weight)`
        Combine two single-asset returns into one composite. Caller fetches
        the per-asset numbers; this is the math.
    `classify_composite_outcome(composite_return, direction, hit_pct)`
        Returns True (hit), False (miss), or None (unscorable: direction=0).

Why a separate module:
    Mirrors `pipeline.factors` (pure scoring logic, separate from the
    orchestrator that handles DB + price fetches). Same rule 24 hygiene —
    the function is testable without a DB, and the orchestrator side
    (`pipeline/track_record.py:_evaluate_market_one`) keeps the I/O.

Why NOT just hardcode equal weights:
    "No hardcoded values" is a project standing rule. The weights come
    from `settings.market_composite_weight_btc` / `_eth` (validated at
    config load to sum to 1.0). Today's defaults are 0.5/0.5; future
    market-cap-weighted variants land by changing env vars, not code.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

# Reuse the direction-sign type from PR I.2 — single source of truth for
# what "long / short / no-direction" looks like across the pipeline.
from etfpulse.pipeline.factors import DirectionSign

# `Outcome` is the win/loss bit returned to the evaluator. None means
# "unscorable — don't write a SignalOutcome row" (the caller's contract,
# matching how `compute_confirmation` returns None for wait-actions).
Outcome = Literal[True, False]


def weighted_composite_return(
    *,
    btc_return_pct: Decimal,
    eth_return_pct: Decimal,
    btc_weight: Decimal,
    eth_weight: Decimal,
) -> Decimal:
    """Combine BTC and ETH per-asset returns into one composite return.

    Inputs are FRACTIONS (`0.02` = 2%), not percentages — same convention
    as `factors.score_price_factor.pct_change`. Output is also a fraction.

    Caller MUST pre-validate that `btc_weight + eth_weight == Decimal('1.0')`
    (the config-level validator does this at boot — see
    `Settings.model_validator`). We don't re-check here because the cost
    of re-checking on every signal score is non-trivial and the boot
    validator already guarantees the invariant.

    Why pass weights as args rather than reading settings: keeps this
    function pure (no module-level config dependency) so tests can vary
    weights without touching the global settings instance. Same pattern
    as `factors.score_price_factor` accepting `window_hours` / `min_pct`
    as args.
    """
    return btc_return_pct * btc_weight + eth_return_pct * eth_weight


def classify_composite_outcome(
    *,
    composite_return_pct: Decimal,
    direction: DirectionSign,
    hit_pct: Decimal,
) -> Outcome | None:
    """Did the composite move in the trade's direction by at least the
    hit threshold?

    Returns:
        True   — composite return × direction ≥ hit_pct (signal hit target).
        False  — composite return × direction < hit_pct (signal didn't hit).
        None   — `direction == 0` (wait action / no AI / unknown action).
                 Caller MUST skip writing a SignalOutcome row in this case
                 (same convention as `compute_confirmation` returning None).

    `hit_pct` is a positive fraction (e.g. `Decimal('0.02')` = 2%). The
    "signed product" check (`composite * direction >= hit_pct`) handles
    both long and short symmetrically without separate branches: a +3%
    move with direction=+1 produces +3% (hits at 2% threshold); a -3%
    move with direction=-1 also produces +3% (also hits). A +3% move with
    direction=-1 produces -3% (miss — direction mismatch counts as 'didn't
    move in expected direction' rather than as 'hit stop', since MARKET
    signals don't have a stop level).
    """
    if direction == 0:
        return None
    signed_progress = composite_return_pct * direction
    return signed_progress >= hit_pct


__all__ = [
    "Outcome",
    "classify_composite_outcome",
    "weighted_composite_return",
]
