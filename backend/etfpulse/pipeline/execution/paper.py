"""Paper-trade simulation primitives.

A `paper_trade=True` order short-circuits in `submit_new` before any
HTTP call: the orchestrator records a simulated ACK + immediate full
fill, opens (or closes) the corresponding Position, and never touches
the gateway. Paper orders + paper positions are isolated from
reconciliation (the venue has no record of them — see
`positions_{spot,perps}.reconcile_*` filters on `paper_trade=False`).

The slippage model (V1):
  - BUY fills at `requested_price * (1 + bps/10000)` — slipped UP.
  - SELL fills at `requested_price * (1 - bps/10000)` — slipped DOWN.
  - Always full-fill (`fill_size = requested_size`); partial paper
    fills are not modelled in V1.

Limitations (V1, flagged for follow-up):
  - Market orders require a reference spot price; paper currently
    relies on `requested_price` being set. Risk gate already denies
    `missing_requested_price`, so a paper market order is only
    reachable if the orchestrator pre-resolved spot into
    `requested_price` upstream — TODO when D.4 wires market-paper.
  - No simulated slippage variation by order size (impact). A
    real-feel paper engine would scale slippage with notional; V1's
    constant-bps model is the simpler "did the system wire end-to-end"
    sanity check.

Pure functions — no DB, no HTTP. Returns the simulated (fill_price,
fill_size). Caller (orchestrator) is responsible for applying the
fill to Order + Position.
"""

from __future__ import annotations

from decimal import Decimal

from etfpulse.config import settings
from etfpulse.models.order import Order, OrderSide

_BPS_PER_UNIT = Decimal("10000")
_PRICE_QUANTUM = Decimal("0.00000001")  # 8-decimal Numeric precision


def simulate_fill(order: Order) -> tuple[Decimal, Decimal]:
    """Compute the simulated paper fill `(fill_price, fill_size)`.

    Requires `order.requested_price` to be set — risk gate denies
    `missing_requested_price` upstream, so this should always hold by
    the time we get here. Defensive ValueError if it's None.

    Slippage from `settings.execution_paper_slippage_bps`. Zero bps
    means "fill at exactly requested_price" (useful for unit tests
    that pin exact expected values).
    """
    if order.requested_price is None:
        raise ValueError(
            f"simulate_fill: order id={order.id} has no requested_price — "
            "paper market orders require a pre-resolved spot reference"
        )
    if order.requested_size <= 0:
        raise ValueError(f"simulate_fill: order id={order.id} has non-positive requested_size")

    slippage = Decimal(settings.execution_paper_slippage_bps) / _BPS_PER_UNIT

    if order.side == OrderSide.BUY.value:
        fill_price = order.requested_price * (Decimal("1") + slippage)
    elif order.side == OrderSide.SELL.value:
        fill_price = order.requested_price * (Decimal("1") - slippage)
    else:
        raise ValueError(f"simulate_fill: unknown order side {order.side!r}")

    # Quantize to the Numeric(18,8) column precision; if a future
    # column type changes, this constant changes here.
    return fill_price.quantize(_PRICE_QUANTUM), order.requested_size
