"""End-to-end smoke: run a full daily cycle against fixtures, then probe
every Stage 06+ read path through real route handlers.

Uses fixtures (no quota burn). Confirms ingestor → detectors → builder →
regime → outcome eval → API routes all line up against current adapter
shapes and DB schema.
"""

from __future__ import annotations

import asyncio
import os

os.environ.setdefault("SOSOVALUE_USE_FIXTURES", "true")
os.environ.setdefault("RUN_BOT", "false")
os.environ.setdefault("RUN_SCHEDULER", "false")

from sqlalchemy import select  # noqa: E402

from etfpulse.db import async_session  # noqa: E402
from etfpulse.models import ETFFlow, NewsItem, Signal, SignalOutcome  # noqa: E402
from etfpulse.pipeline.regime_monitor import classify_regime, get_latest_regime  # noqa: E402
from etfpulse.pipeline.signal_builder import run_daily_cycle  # noqa: E402
from etfpulse.pipeline.track_record import (  # noqa: E402
    evaluate_pending_outcomes,
    get_recent_outcomes,
    get_stats_by_confidence_floor,
)


async def main() -> None:
    print("=== E2E SMOKE ===\n")

    # 1. Daily cycle (ingest + detect + build + ai)
    print("[1] run_daily_cycle()")
    async with async_session() as s:
        summary = await run_daily_cycle(s)
        await s.commit()
    print(f"   ingested: {summary.get('ingested')}")
    print(f"   prices:   {summary.get('prices')}")
    print(f"   signals_built: {summary.get('signals_built')}")
    print(f"   detector_errors: {summary.get('detector_errors')}")
    print(f"   ingest_errors:   {summary.get('ingest_errors')}")

    # 2. Inspect persisted state
    print("\n[2] persisted state")
    async with async_session() as s:
        flows = (await s.execute(select(ETFFlow))).scalars().all()
        news = (await s.execute(select(NewsItem))).scalars().all()
        signals = (await s.execute(select(Signal))).scalars().all()
        outcomes = (await s.execute(select(SignalOutcome))).scalars().all()
    print(f"   ETFFlow rows:      {len(flows)}")
    print(f"   NewsItem rows:     {len(news)}")
    print(f"   Signal rows:       {len(signals)}")
    print(f"   SignalOutcome:     {len(outcomes)}")
    if signals:
        s0 = signals[0]
        print(
            f"   sample signal: id={s0.id} type={s0.signal_type} asset={s0.asset} "
            f"date={s0.signal_date} conf={s0.confidence} status={s0.status} "
            f"price@create={s0.price_at_creation}"
        )
        print(f"   entry/stop/target: {s0.entry_price} / {s0.stop_price} / {s0.target_price}")

    # 3. Regime classification
    print("\n[3] classify_regime()")
    async with async_session() as s:
        regime = await classify_regime(s)
        await s.commit()
    print(f"   {regime}")
    async with async_session() as s:
        latest = await get_latest_regime(s)
    print(
        f"   latest snapshot in DB: regime={latest.regime if latest else None} confidence={latest.confidence if latest else None}"  # noqa: E501
    )

    # 4. Outcome eval
    print("\n[4] evaluate_pending_outcomes()")
    async with async_session() as s:
        eval_summary = await evaluate_pending_outcomes(s)
        await s.commit()
    print(f"   {eval_summary}")

    # 5. Track-record stats (would feed the public page + bot /track-record)
    print("\n[5] track-record stats")
    async with async_session() as s:
        stats = await get_stats_by_confidence_floor(s)
        recent = await get_recent_outcomes(s, limit=5)
    print(f"   {stats}")
    print(f"   recent outcomes: {len(recent)}")

    # 6. Hit the actual FastAPI routes via TestClient
    print("\n[6] HTTP routes")
    from fastapi.testclient import TestClient

    from etfpulse.app import create_app

    client = TestClient(create_app())
    for path in (
        "/api/health",
        "/api/health/ready",
        "/api/dashboard/stats",
        "/api/regime",
        "/api/signals?limit=5",
        "/api/track-record?limit=5",
    ):
        r = client.get(path)
        body = (
            r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text
        )
        if isinstance(body, dict):
            preview = {
                k: (type(v).__name__ if isinstance(v, (list, dict)) else v)
                for k, v in list(body.items())[:6]
            }
        else:
            preview = body
        print(f"   GET {path:40s} -> {r.status_code}  {preview}")

    print("\n=== done ===")


if __name__ == "__main__":
    asyncio.run(main())
