"""PR I.2 — confirmation backfill tests.

Focus: the orchestrator's per-signal categorisation logic (no-direction
skip, unsupported-asset skip, regime fallback, error catch-and-continue).
The factor math is pinned in `test_factors.py`; here we verify the
backfill wires the right inputs to it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from etfpulse.models import MarketRegime, RegimeSnapshot, Signal, SignalPosture
from etfpulse.pipeline.confirmation_backfill import backfill_null_confirmation
from etfpulse.pipeline.detectors import compute_fingerprint


async def _seed_signal(
    db_session,
    *,
    key: str,
    suggested_action: str | None = "consider long",
    confidence: int | None = 7,
    asset: str = "BTC",
    price_source: str | None = "binance",
    regime_at_creation: str | None = "markup",
    created_offset_hours: int = 80,
) -> Signal:
    """Seed a historical signal eligible for backfill scoring.

    `regime_at_creation=None` produces a signal WITHOUT the trigger_data
    regime block — exercises the `_closest_regime_snapshot` fallback.
    """
    t0 = datetime.now(UTC) - timedelta(hours=created_offset_hours)
    trigger_data: dict = {"streak_days": 4}
    if regime_at_creation is not None:
        trigger_data["regime_at_creation"] = {
            "regime": regime_at_creation,
            "signal_posture": "normal",
            "confidence": 7,
            "macro_events_nearby": [],
            "reasoning": {},
            "btc_dominance": None,
        }
    signal = Signal(
        signal_type="flow_anomaly",
        asset=asset,
        trigger_data=trigger_data,
        ai_analysis=(
            {"suggested_action": suggested_action, "headline": "x"}
            if suggested_action is not None
            else None
        ),
        confidence=confidence,
        status="alerted",
        price_at_creation=Decimal("100"),
        price_source=price_source,
        ai_prompt_version="v3",
        fingerprint=compute_fingerprint("backfill-test", key),
        signal_date=t0.date(),
    )
    signal.created_at = t0
    db_session.add(signal)
    await db_session.flush()
    return signal


@pytest.fixture
def stub_klines(monkeypatch):
    """Patch the factor's klines fetcher to return canned bars anchored
    near `now()`. Tests override `state["bars"]` per-test.

    Anchoring to `now()` matters: the seeded signal's `created_at` is
    `now() - 80h`, and the price factor looks at bars in
    `[created_at - 24h, created_at]`. We generate bars in that window.
    """
    state: dict = {"bars": []}

    async def _fetch(asset, source, *, start_time_ms=None, end_time_ms=None, limit=100):
        return list(state["bars"])

    monkeypatch.setattr("etfpulse.pipeline.factors.get_daily_klines_from_source", _fetch)
    return state


def _bars_anchored_to(reference: datetime, close_pair: tuple[str, str]):
    """Build the two-bar fixture the price factor expects, anchored to
    the seeded signal's `created_at` (mirrors how the production cycle
    sees bars near the signal's reference_time)."""
    from etfpulse.pipeline.prices import PriceBar

    ts0 = int((reference - timedelta(hours=24)).timestamp() * 1000)
    ts1 = int(reference.timestamp() * 1000)
    return [
        PriceBar(
            timestamp_ms=ts0,
            open=Decimal(close_pair[0]),
            high=Decimal(close_pair[0]),
            low=Decimal(close_pair[0]),
            close=Decimal(close_pair[0]),
        ),
        PriceBar(
            timestamp_ms=ts1,
            open=Decimal(close_pair[1]),
            high=Decimal(close_pair[1]),
            low=Decimal(close_pair[1]),
            close=Decimal(close_pair[1]),
        ),
    ]


class TestBackfillHappyPath:
    async def test_scores_signal_with_trigger_data_regime(self, db_session, stub_klines):
        signal = await _seed_signal(db_session, key="happy")
        stub_klines["bars"] = _bars_anchored_to(signal.created_at, ("100", "102"))

        summary = await backfill_null_confirmation(db_session)
        assert summary["candidates"] == 1
        assert summary["updated"] == 1

        await db_session.refresh(signal)
        assert signal.confirmation_score == 2  # price + regime; news=0
        assert signal.factor_votes is not None
        assert signal.factor_votes["price"]["vote"] == 1
        assert signal.factor_votes["regime"]["vote"] == 1
        assert signal.factor_votes["news"]["vote"] == 0

    async def test_falls_back_to_regime_snapshot_when_trigger_data_lacks_block(
        self, db_session, stub_klines
    ):
        # Pre-A.1 signals don't have trigger_data.regime_at_creation.
        # Seed a snapshot BEFORE the signal so the fallback succeeds.
        signal = await _seed_signal(db_session, key="legacy-no-block", regime_at_creation=None)
        # Snapshot 1h before signal — should be found by the fallback.
        snapshot = RegimeSnapshot(
            captured_at=signal.created_at - timedelta(hours=1),
            regime=MarketRegime.MARKUP.value,
            signal_posture=SignalPosture.NORMAL.value,
            confidence=7,
            reasoning={},
            btc_dominance=None,
        )
        db_session.add(snapshot)
        await db_session.flush()
        stub_klines["bars"] = _bars_anchored_to(signal.created_at, ("100", "102"))

        summary = await backfill_null_confirmation(db_session)
        assert summary["updated"] == 1

        await db_session.refresh(signal)
        # Regime fallback found MARKUP → vote +1; price +1; news 0 → score 2.
        assert signal.confirmation_score == 2

    async def test_no_regime_anywhere_falls_back_to_vote_zero(self, db_session, stub_klines):
        # No trigger_data block + no RegimeSnapshot → regime factor votes 0.
        # Price still confirms (+1); news 0; → score 1.
        signal = await _seed_signal(db_session, key="no-regime", regime_at_creation=None)
        stub_klines["bars"] = _bars_anchored_to(signal.created_at, ("100", "103"))

        summary = await backfill_null_confirmation(db_session)
        assert summary["updated"] == 1

        await db_session.refresh(signal)
        assert signal.confirmation_score == 1


class TestBackfillSkipCategories:
    async def test_wait_action_skipped(self, db_session, stub_klines):
        signal = await _seed_signal(db_session, key="wait", suggested_action="wait")

        summary = await backfill_null_confirmation(db_session)
        assert summary["candidates"] == 1
        assert summary["skipped_no_direction"] == 1
        assert summary["updated"] == 0

        await db_session.refresh(signal)
        assert signal.confirmation_score is None

    async def test_market_asset_skipped_as_unsupported(self, db_session, stub_klines):
        # PR F.3 — regime_shift MARKET sentinels carry asset="MARKET".
        # No single asset to score against → skip.
        signal = await _seed_signal(db_session, key="market", asset="MARKET")

        summary = await backfill_null_confirmation(db_session)
        assert summary["candidates"] == 1
        assert summary["skipped_unsupported_asset"] == 1
        assert summary["updated"] == 0

        await db_session.refresh(signal)
        assert signal.confirmation_score is None

    async def test_ai_failed_signal_excluded_from_candidates(self, db_session, stub_klines):
        # AI-failed signals (NULL ai_analysis) are filtered at the SQL level
        # — they were never going to score under any factor combination.
        await _seed_signal(db_session, key="ai-fail", suggested_action=None, confidence=None)

        summary = await backfill_null_confirmation(db_session)
        # Not a candidate; the candidate filter requires ai_analysis IS NOT NULL.
        assert summary["candidates"] == 0

    async def test_already_scored_signal_excluded(self, db_session, stub_klines):
        # Idempotent: signals with non-NULL confirmation_score aren't candidates.
        signal = await _seed_signal(db_session, key="already")
        signal.confirmation_score = 1
        await db_session.flush()

        summary = await backfill_null_confirmation(db_session)
        assert summary["candidates"] == 0


class TestBackfillLimit:
    async def test_limit_caps_batch_size(self, db_session, stub_klines):
        for i in range(5):
            await _seed_signal(db_session, key=f"limit-{i}")
        stub_klines["bars"] = _bars_anchored_to(
            datetime.now(UTC) - timedelta(hours=80), ("100", "102")
        )

        summary = await backfill_null_confirmation(db_session, limit=2)
        assert summary["candidates"] == 2
        assert summary["updated"] == 2

        rows_with_score = (
            (await db_session.execute(select(Signal).where(Signal.confirmation_score.is_not(None))))
            .scalars()
            .all()
        )
        assert len(rows_with_score) == 2

    async def test_unlimited_processes_full_backlog(self, db_session, stub_klines):
        for i in range(3):
            await _seed_signal(db_session, key=f"unlim-{i}")
        stub_klines["bars"] = _bars_anchored_to(
            datetime.now(UTC) - timedelta(hours=80), ("100", "102")
        )

        summary = await backfill_null_confirmation(db_session)
        assert summary["candidates"] == 3
        assert summary["updated"] == 3


class TestBackfillErrorHandling:
    async def test_one_bad_signal_does_not_kill_the_run(self, db_session, stub_klines, monkeypatch):
        # D13 — per-signal try/except: a single bad signal must not abort
        # the rest. Patch `compute_confirmation` to raise on a SPECIFIC
        # asset, succeed on others.
        good = await _seed_signal(db_session, key="good")
        bad = await _seed_signal(db_session, key="bad", asset="BTC")
        # Both default to BTC; differentiate via a per-signal patch.

        from etfpulse.pipeline import confirmation_backfill

        real_score_one = confirmation_backfill._score_one

        async def _flaky(session, signal, summary):
            if signal.id == bad.id:
                raise RuntimeError("simulated factor failure")
            return await real_score_one(session, signal, summary)

        monkeypatch.setattr(confirmation_backfill, "_score_one", _flaky)
        stub_klines["bars"] = _bars_anchored_to(good.created_at, ("100", "102"))

        summary = await backfill_null_confirmation(db_session)
        assert summary["candidates"] == 2
        assert summary["updated"] == 1  # good one
        assert summary["errored"] == 1  # bad one

        await db_session.refresh(good)
        await db_session.refresh(bad)
        assert good.confirmation_score is not None
        assert bad.confirmation_score is None


class TestBackfillNoCommit:
    async def test_no_commit_in_orchestrator(self, db_session, stub_klines):
        # D14: orchestrator MUST NOT call session.commit(). Verified by
        # `in_transaction()` still True after the call (the conftest's
        # per-test transaction is still open).
        await _seed_signal(db_session, key="commit-check")
        stub_klines["bars"] = _bars_anchored_to(
            datetime.now(UTC) - timedelta(hours=80), ("100", "102")
        )
        await backfill_null_confirmation(db_session)
        assert db_session.in_transaction() is True
