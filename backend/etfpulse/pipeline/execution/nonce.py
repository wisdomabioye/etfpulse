"""Per-user monotonic millisecond nonce generation.

The SoDEX gateway accepts EIP-712 nonces within `(T-2d, T+1d)` ms (per
api.md). Within that window, a (wallet, nonce) pair must be unique —
the gateway tracks consumed nonces per signer and rejects replays.

Our nonce strategy: per-user `max(now_ms, last_nonce + 1)`. Properties:

  - Monotonically increasing per user — no collision within our DB.
  - Tied to wall-clock — recoverable from gateway logs by timestamp.
  - Never reused (within practical time horizons; 1ms granularity ×
    1 day window = 86.4M slots, far beyond bot-rate submission).

CONCURRENCY CONTRACT (load-bearing): callers MUST hold
`SELECT ... FOR UPDATE` on the User row before calling this function.
Without that lock:

  - Two concurrent `prepare_order` calls for the same user could each
    read `MAX(Order.nonce)` BEFORE either INSERT lands.
  - Both compute the same `last + 1` → identical nonces.
  - The gateway rejects the second (signature replay error).

The risk-controller in `risk.py` opens the FOR UPDATE; the orchestrator
in `pipeline.py` chains: lock → risk check → nonce → builder → INSERT
→ commit. The lock releases on commit.

Scale follow-up (flagged for next-stage work; tracked in CLAUDE.md
"Scale follow-ups"): under bot-rate submission from a SINGLE user
(>1/s sustained), the FOR UPDATE lock serialises every submit through
this function. Fix path: dedicated `nonce_counters` table with atomic
`UPDATE … RETURNING` (no table lock, per-row contention only).
"""

from __future__ import annotations

import time

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from etfpulse.models.order import Order


async def next_nonce_for_user(session: AsyncSession, user_id: int) -> int:
    """Return the next monotonic ms nonce for `user_id`.

    PRECONDITION: caller holds `SELECT ... FOR UPDATE` on the User row
    matching `user_id`. See module docstring for the contract.

    Returns `max(now_ms, last_user_nonce + 1)`. The `+1` guarantees
    strict monotonicity even within the same millisecond (relevant for
    fast-fire test paths; in production the wall-clock branch
    dominates).
    """
    row = await session.execute(select(func.max(Order.nonce)).where(Order.user_id == user_id))
    last = row.scalar() or 0
    now_ms = int(time.time() * 1000)
    return max(now_ms, last + 1)
