"""Boot-time diagnostics for orphan SUBMITTED orders.

If the FastAPI process crashed between `session.commit()` of the
PENDING→SUBMITTED transition and the HTTP POST to the SoDEX gateway,
the Order row will be SUBMITTED locally but the gateway may or may
not have received the request. The reconcile loop resolves the
ambiguity (queries `/orders/open` by clOrdID); this module's job is
just to SURFACE the orphan count at boot so operators see it in
deploy logs.

No mutation — reconcile owns the recovery. This is purely a log +
metric for operational visibility. Trying to "force" reconcile here
would race with the scheduler that's about to register the same
reconcile job.

D14: no commit. Returns the orphan count.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from etfpulse.config import settings
from etfpulse.models.order import Order, OrderStatus

log = structlog.get_logger(__name__)


async def flag_orphan_submitted_on_boot(session: AsyncSession) -> dict[str, int]:
    """Count + log SUBMITTED orders that the gateway may have lost
    (no exchange_order_id yet, and the row hasn't been touched in
    long enough that reconcile should have caught it by now).

    "Long enough" = `2 * order_reconcile_interval_seconds` — gives the
    in-process reconcile two ticks of grace before we flag the row.
    Set lower than `nonce_expires_at` (1 day) so this is a much
    earlier signal than the expiry reaper.

    Doesn't fix anything — reconcile is the fix. Just logs.
    """
    threshold = datetime.now(UTC) - timedelta(seconds=2 * settings.order_reconcile_interval_seconds)
    stmt = (
        select(func.count(Order.id))
        .where(Order.status == OrderStatus.SUBMITTED.value)
        .where(Order.exchange_order_id.is_(None))
        .where(Order.updated_at < threshold)
    )
    orphan_count = int((await session.execute(stmt)).scalar_one())
    summary = {"orphan_submitted": orphan_count}

    if orphan_count > 0:
        log.warning(
            "boot_orphan_submitted_orders",
            count=orphan_count,
            stale_threshold_seconds=2 * settings.order_reconcile_interval_seconds,
            note="reconcile will resolve on next sweep; surfacing for operator visibility",
        )
    else:
        log.info("boot_orphan_submitted_check_clean")

    return summary
