"""GET /api/dashboard/stats aggregation tests.

Pattern: same dependency-override + AsyncClient/ASGITransport as test_signals.
Seeds signals via db_session, asserts on the aggregated response.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from httpx import ASGITransport

from etfpulse.api.deps import get_db_session
from etfpulse.app import create_app
from etfpulse.models import (
    MarketRegime,
    RegimeSnapshot,
    Signal,
    SignalOutcome,
    SignalPosture,
)
from etfpulse.pipeline.detectors import compute_fingerprint


@pytest.fixture
async def client(db_session):
    app = create_app()

    async def _override() -> AsyncIterator:
        yield db_session

    app.dependency_overrides[get_db_session] = _override
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def _seed_signal(
    db_session,
    *,
    confidence: int | None = 7,
    created_at: datetime | None = None,
    key: str = "x",
) -> Signal:
    created_at = created_at or datetime.now(UTC)
    signal = Signal(
        signal_type="flow_anomaly",
        asset="BTC",
        trigger_data={},
        confidence=confidence,
        status="alerted",
        fingerprint=compute_fingerprint("stats-test", key, str(created_at.timestamp())),
        signal_date=date(2026, 4, 22),
    )
    db_session.add(signal)
    await db_session.flush()
    # Override server_default created_at for deterministic tests.
    signal.created_at = created_at
    await db_session.flush()
    return signal


class TestDashboardStats:
    async def test_empty_db_returns_zero_state(self, db_session, client):
        """Clean install: no signals → all fields at their empty-state defaults."""
        r = await client.get("/api/dashboard/stats")
        assert r.status_code == 200
        body = r.json()
        assert body == {
            "total_signals": 0,
            "signals_today": 0,
            "avg_confidence": None,
            "last_signal_at": None,
            # Stage 7-P7: regime fields null until the first cycle runs.
            "current_regime": None,
            "signal_posture": None,
            # PR B (#60) — `hit_rate_global` is the v2 name; `hit_rate_72h`
            # stays for one deprecation cycle and carries the same value.
            # Both null + 0 evaluated until signals age past their window.
            "hit_rate_global": None,
            "hit_rate_72h": None,
            "evaluated_count": 0,
            # PR E.1 — hero card slots are null on a cold DB. FE shows the
            # aggregate strip alone in this state.
            "last_target_hit": None,
            "last_stop_saved": None,
        }

    async def test_total_signals_counts_all(self, db_session, client):
        for i in range(3):
            await _seed_signal(db_session, key=f"t{i}")

        r = await client.get("/api/dashboard/stats")
        assert r.json()["total_signals"] == 3

    async def test_signals_today_respects_utc_midnight(self, db_session, client):
        """Seed a signal from yesterday UTC and a signal from 00:00 UTC today.
        Only today's should count toward signals_today."""
        now = datetime.now(UTC)
        today_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)

        # Yesterday (23:59 yesterday UTC) — NOT counted.
        await _seed_signal(
            db_session,
            created_at=today_midnight - timedelta(minutes=1),
            key="yesterday",
        )
        # Exactly 00:00:00 UTC today — counted (>= today_midnight).
        await _seed_signal(db_session, created_at=today_midnight, key="midnight")
        # Just after — counted.
        await _seed_signal(
            db_session,
            created_at=today_midnight + timedelta(hours=3),
            key="morning",
        )

        r = await client.get("/api/dashboard/stats")
        body = r.json()
        assert body["total_signals"] == 3
        assert body["signals_today"] == 2

    async def test_avg_confidence_ignores_null(self, db_session, client):
        """Signals with confidence=None (AI-failed) must NOT drag the average."""
        await _seed_signal(db_session, confidence=None, key="null1")
        await _seed_signal(db_session, confidence=None, key="null2")
        await _seed_signal(db_session, confidence=6, key="c6")
        await _seed_signal(db_session, confidence=8, key="c8")

        r = await client.get("/api/dashboard/stats")
        body = r.json()
        assert body["total_signals"] == 4
        # Average over the two non-null: (6 + 8) / 2 = 7.0
        assert body["avg_confidence"] == 7.0

    async def test_avg_confidence_none_when_all_null(self, db_session, client):
        """AVG over all-NULL column → NULL → serialized as None."""
        await _seed_signal(db_session, confidence=None, key="n1")
        await _seed_signal(db_session, confidence=None, key="n2")

        r = await client.get("/api/dashboard/stats")
        body = r.json()
        assert body["total_signals"] == 2
        assert body["avg_confidence"] is None

    async def test_avg_confidence_returned_as_float(self, db_session, client):
        """Postgres AVG returns Decimal; we cast to float for JSON. Verify
        this explicitly — a Decimal in the response would break JS clients."""
        await _seed_signal(db_session, confidence=7, key="f1")

        r = await client.get("/api/dashboard/stats")
        body = r.json()
        assert isinstance(body["avg_confidence"], float)
        assert body["avg_confidence"] == 7.0

    async def test_last_signal_at_matches_max_created_at(self, db_session, client):
        now = datetime.now(UTC).replace(microsecond=0)
        await _seed_signal(db_session, created_at=now - timedelta(hours=3), key="old")
        await _seed_signal(db_session, created_at=now - timedelta(hours=1), key="mid")
        await _seed_signal(db_session, created_at=now, key="newest")

        r = await client.get("/api/dashboard/stats")
        body = r.json()
        # ISO parse and compare — allow for minor serialization tolerance.
        assert body["last_signal_at"] is not None
        parsed = datetime.fromisoformat(body["last_signal_at"])
        assert parsed == now

    async def test_returns_all_documented_fields(self, db_session, client):
        """Locks the response contract — frontend depends on exactly these keys."""
        r = await client.get("/api/dashboard/stats")
        body = r.json()
        assert set(body.keys()) == {
            "total_signals",
            "signals_today",
            "avg_confidence",
            "last_signal_at",
            "current_regime",
            "signal_posture",
            # PR E.1 — hero card slots (`HeroOutcome | None`).
            "last_target_hit",
            "last_stop_saved",
            # PR B (#60) — `hit_rate_global` is v2; `hit_rate_72h` is the
            # deprecated parallel kept for one release cycle.
            "hit_rate_global",
            "hit_rate_72h",
            "evaluated_count",
        }

    async def test_current_regime_populated_when_snapshot_exists(self, db_session, client):
        """When a `regime_snapshots` row exists, the home stats surface it
        without a second roundtrip to /api/regime."""
        db_session.add(
            RegimeSnapshot(
                captured_at=datetime.now(UTC),
                regime=MarketRegime.MARKUP.value,
                signal_posture=SignalPosture.NORMAL.value,
                confidence=8,
            )
        )
        await db_session.flush()

        r = await client.get("/api/dashboard/stats")
        body = r.json()
        assert body["current_regime"] == "markup"
        assert body["signal_posture"] == "normal"

    async def test_current_regime_uses_latest_snapshot(self, db_session, client):
        """Multiple snapshots → newest wins (matches /api/regime semantics
        + uses the same `get_latest_regime` helper)."""
        now = datetime.now(UTC)
        db_session.add(
            RegimeSnapshot(
                captured_at=now - timedelta(days=1),
                regime=MarketRegime.ACCUMULATION.value,
                signal_posture=SignalPosture.AGGRESSIVE.value,
                confidence=5,
            )
        )
        db_session.add(
            RegimeSnapshot(
                captured_at=now,
                regime=MarketRegime.DISTRIBUTION.value,
                signal_posture=SignalPosture.CAUTIOUS.value,
                confidence=6,
            )
        )
        await db_session.flush()

        r = await client.get("/api/dashboard/stats")
        body = r.json()
        assert body["current_regime"] == "distribution"
        assert body["signal_posture"] == "cautious"

    async def test_current_regime_null_for_legacy_snapshot(self, db_session, client):
        """Pre-Stage-7 snapshot (regime IS NULL) → fields surface as null,
        not the raw column value."""
        # Legacy row with no regime/posture columns populated.
        db_session.add(RegimeSnapshot(captured_at=datetime.now(UTC)))
        await db_session.flush()

        r = await client.get("/api/dashboard/stats")
        body = r.json()
        assert body["current_regime"] is None
        assert body["signal_posture"] is None


# ---------------------------------------------------------------------------
# Stage 8-P5 — hit_rate_72h + evaluated_count (closes #44)
# ---------------------------------------------------------------------------


async def _seed_outcome(
    db_session,
    *,
    confidence: int = 7,
    hit_target: bool | None = True,
    key: str = "x",
) -> SignalOutcome:
    """Seed a Signal + matching SignalOutcome — what `/api/dashboard/stats`
    aggregates over for hit_rate_72h. Mirrors the helper in test_track_record."""
    signal = Signal(
        signal_type="flow_anomaly",
        asset="BTC",
        trigger_data={},
        ai_analysis={"suggested_action": "consider long", "headline": "x"},
        confidence=confidence,
        status="alerted",
        price_at_creation=Decimal("84200"),
        price_source="binance",
        ai_prompt_version="v3",
        fingerprint=compute_fingerprint("dashboard-hit-rate", key),
        signal_date=date(2026, 4, 25),
    )
    db_session.add(signal)
    await db_session.flush()

    outcome = SignalOutcome(
        signal_id=signal.id,
        asset="BTC",
        signal_type="flow_anomaly",
        direction="long",
        confidence=confidence,
        entry_price=Decimal("84200"),
        target_price=Decimal("89500"),
        price_at_signal=Decimal("84200"),
        hit_target=hit_target,
        evaluated_at=datetime.now(UTC),
    )
    db_session.add(outcome)
    await db_session.flush()
    return outcome


class TestHitRate72h:
    async def test_hit_rate_computed_as_percent(self, db_session, client):
        """3 hits + 1 miss out of 4 with-target outcomes = 75%. PERCENT
        unit (0..100), not fraction (0..1) — same as track-record API so
        the FE never converts."""
        for i in range(3):
            await _seed_outcome(db_session, hit_target=True, key=f"hit{i}")
        await _seed_outcome(db_session, hit_target=False, key="miss")

        body = (await client.get("/api/dashboard/stats")).json()
        assert body["hit_rate_72h"] == 75.0
        assert body["evaluated_count"] == 4

    async def test_hit_rate_excludes_no_target_signals_from_denominator(self, db_session, client):
        """Same rationale as `/api/track-record` — signals where AI declined
        a target (hit_target IS NULL) shouldn't dilute the rate."""
        await _seed_outcome(db_session, hit_target=True, key="hit")
        await _seed_outcome(db_session, hit_target=None, key="no-target-1")
        await _seed_outcome(db_session, hit_target=None, key="no-target-2")

        body = (await client.get("/api/dashboard/stats")).json()
        # 1 hit / 1 with-target = 100%, NOT 1/3 = 33%
        assert body["hit_rate_72h"] == 100.0
        # All three outcome rows count toward `evaluated_count` though.
        assert body["evaluated_count"] == 3

    async def test_hit_rate_none_when_no_targets_set(self, db_session, client):
        """All outcomes have NULL hit_target → no signal had a target →
        hit_rate is undefined → null. Better than rendering '0%' on the
        home tile for an empty cohort."""
        await _seed_outcome(db_session, hit_target=None, key="n1")
        await _seed_outcome(db_session, hit_target=None, key="n2")

        body = (await client.get("/api/dashboard/stats")).json()
        assert body["hit_rate_72h"] is None
        assert body["evaluated_count"] == 2

    async def test_hit_rate_returned_as_float(self, db_session, client):
        """Pin the wire type — frontend expects `number | null`. A string
        would silently break the panel's `Math.round` call."""
        await _seed_outcome(db_session, hit_target=True, key="t")

        body = (await client.get("/api/dashboard/stats")).json()
        assert isinstance(body["hit_rate_72h"], float)

    async def test_outcome_with_null_evaluated_at_excluded(self, db_session, client):
        """Defensive — `evaluated_at IS NOT NULL` in the WHERE matches
        the track-record endpoint's filter, so a future writer leaking a
        NULL evaluated_at row doesn't pollute the home tile."""
        # Manually insert a Signal + outcome with evaluated_at=None.
        signal = Signal(
            signal_type="flow_anomaly",
            asset="BTC",
            trigger_data={},
            ai_analysis={"suggested_action": "consider long", "headline": "x"},
            confidence=7,
            status="alerted",
            price_at_creation=Decimal("84200"),
            price_source="binance",
            ai_prompt_version="v3",
            fingerprint=compute_fingerprint("dashboard-hit-rate", "no-eval"),
            signal_date=date(2026, 4, 25),
        )
        db_session.add(signal)
        await db_session.flush()
        db_session.add(
            SignalOutcome(
                signal_id=signal.id,
                asset="BTC",
                signal_type="flow_anomaly",
                direction="long",
                confidence=7,
                target_price=Decimal("89500"),
                price_at_signal=Decimal("84200"),
                hit_target=True,
                evaluated_at=None,  # explicitly unevaluated
            )
        )
        await db_session.flush()

        body = (await client.get("/api/dashboard/stats")).json()
        assert body["evaluated_count"] == 0
        assert body["hit_rate_72h"] is None


# ---------------------------------------------------------------------------
# Hero outcome card (PR E.1 / task #28)
# ---------------------------------------------------------------------------


async def _seed_signal_with_levels(
    db_session,
    *,
    key: str,
    headline: str = "Test headline",
    confidence: int = 7,
    asset: str = "BTC",
    signal_type: str = "flow_anomaly",
    created_at: datetime | None = None,
) -> Signal:
    """Seed a Signal with the price-level fields populated. Required for
    SignalOutcome rows that surface in the hero card — both `last_target_hit`
    and `last_stop_saved` filter on entry_price/target_price/stop_price."""
    created = created_at or datetime.now(UTC)
    signal = Signal(
        signal_type=signal_type,
        asset=asset,
        trigger_data={},
        ai_analysis={
            "headline": headline,
            "suggested_action": "consider long",
            "time_horizon": "swing",
        },
        confidence=confidence,
        status="alerted",
        price_at_creation=Decimal("84200"),
        price_source="binance",
        ai_prompt_version="v3",
        fingerprint=compute_fingerprint("hero-test", key),
        signal_date=date(2026, 4, 25),
        entry_price=Decimal("84200"),
        stop_price=Decimal("82000"),
        target_price=Decimal("88000"),
    )
    db_session.add(signal)
    await db_session.flush()
    signal.created_at = created
    await db_session.flush()
    return signal


# Sentinel for `_seed_hero_outcome.evaluated_at` so tests can distinguish
# "use default (now)" from "explicit NULL." `None` alone would conflate the
# two via `value or default`.
_UNSET = object()


async def _seed_hero_outcome(
    db_session,
    *,
    signal: Signal,
    hit_target: bool | None = None,
    hit_stop: bool | None = None,
    max_favorable: Decimal | None = Decimal("0.05"),
    max_adverse: Decimal | None = Decimal("0.02"),
    evaluated_at: datetime | None | object = _UNSET,
    direction: str = "long",
    entry_price: Decimal | None = Decimal("84200"),
    stop_price: Decimal | None = Decimal("82000"),
    target_price: Decimal | None = Decimal("88000"),
) -> SignalOutcome:
    """Seed a SignalOutcome with the levels + max_* fields the hero filters
    require. Defaults populate the columns; tests pass None to exercise the
    skip paths. Named `_seed_hero_outcome` (not `_seed_outcome`) to avoid
    colliding with `TestHitRate72h`'s module-level helper of the same name.
    """
    resolved_evaluated_at = datetime.now(UTC) if evaluated_at is _UNSET else evaluated_at
    outcome = SignalOutcome(
        signal_id=signal.id,
        asset=signal.asset,
        signal_type=signal.signal_type,
        direction=direction,
        confidence=signal.confidence or 7,
        entry_price=entry_price,
        stop_price=stop_price,
        target_price=target_price,
        price_at_signal=Decimal("84200"),
        hit_target=hit_target,
        hit_stop=hit_stop,
        max_favorable=max_favorable,
        max_adverse=max_adverse,
        evaluated_at=resolved_evaluated_at,
    )
    db_session.add(outcome)
    await db_session.flush()
    return outcome


class TestHeroOutcome:
    """`last_target_hit` + `last_stop_saved` selection rules.

    Backend stays neutral on framing — surfaces the latest qualifying
    outcome by `evaluated_at` (no curation, no survivorship bias). FE
    decides whether to display, rotate, or fall back to aggregate-only.
    """

    async def test_returns_latest_target_hit_when_multiple_exist(self, db_session, client):
        """Among multiple target-hit outcomes, the most recent by
        `evaluated_at` wins. Pins the no-cherry-pick rule."""
        old_sig = await _seed_signal_with_levels(db_session, key="t-old", headline="Old win")
        new_sig = await _seed_signal_with_levels(db_session, key="t-new", headline="Fresh win")
        await _seed_hero_outcome(
            db_session,
            signal=old_sig,
            hit_target=True,
            evaluated_at=datetime.now(UTC) - timedelta(days=10),
        )
        await _seed_hero_outcome(
            db_session,
            signal=new_sig,
            hit_target=True,
            evaluated_at=datetime.now(UTC) - timedelta(days=1),
        )

        body = (await client.get("/api/dashboard/stats")).json()
        hero = body["last_target_hit"]
        assert hero is not None
        assert hero["signal_id"] == new_sig.id
        assert hero["headline"] == "Fresh win"
        # Fractions, not percentages — naming intentionally drops `_pct` (PR E.1).
        assert Decimal(hero["max_favorable"]) == Decimal("0.05")

    async def test_returns_latest_stop_saved(self, db_session, client):
        """Mirror of target_hit on the hit_stop=True path. Old stop is
        ignored in favour of the most recent."""
        old_sig = await _seed_signal_with_levels(db_session, key="s-old")
        new_sig = await _seed_signal_with_levels(db_session, key="s-new")
        await _seed_hero_outcome(
            db_session,
            signal=old_sig,
            hit_stop=True,
            max_adverse=Decimal("0.04"),
            evaluated_at=datetime.now(UTC) - timedelta(days=5),
        )
        await _seed_hero_outcome(
            db_session,
            signal=new_sig,
            hit_stop=True,
            max_adverse=Decimal("0.07"),
            evaluated_at=datetime.now(UTC) - timedelta(hours=2),
        )

        body = (await client.get("/api/dashboard/stats")).json()
        hero = body["last_stop_saved"]
        assert hero is not None
        assert hero["signal_id"] == new_sig.id
        assert Decimal(hero["max_adverse"]) == Decimal("0.07")

    async def test_target_hit_and_stop_saved_picked_independently(self, db_session, client):
        """Independent slots — different signals can fill each card. Pins
        that the two queries don't share state."""
        win_sig = await _seed_signal_with_levels(db_session, key="indep-win", headline="A win")
        save_sig = await _seed_signal_with_levels(db_session, key="indep-save", headline="A save")
        await _seed_hero_outcome(db_session, signal=win_sig, hit_target=True)
        await _seed_hero_outcome(db_session, signal=save_sig, hit_stop=True)

        body = (await client.get("/api/dashboard/stats")).json()
        assert body["last_target_hit"]["signal_id"] == win_sig.id
        assert body["last_target_hit"]["headline"] == "A win"
        assert body["last_stop_saved"]["signal_id"] == save_sig.id
        assert body["last_stop_saved"]["headline"] == "A save"

    async def test_skips_outcome_without_entry_price(self, db_session, client):
        """Legacy rows that pre-date Stage 08 may lack entry_price. They
        can't anchor a "could have made/saved" framing — must be filtered
        out at the SQL level so they never appear in the hero slot."""
        sig = await _seed_signal_with_levels(db_session, key="no-entry")
        await _seed_hero_outcome(
            db_session,
            signal=sig,
            hit_target=True,
            entry_price=None,  # legacy row
            stop_price=None,
            target_price=None,
        )

        body = (await client.get("/api/dashboard/stats")).json()
        assert body["last_target_hit"] is None

    async def test_skips_stop_saved_with_null_max_adverse(self, db_session, client):
        """`max_adverse IS NULL` shouldn't qualify. Hero framing requires
        a real underwater number to compute "would have lost X%."""
        sig = await _seed_signal_with_levels(db_session, key="null-adv")
        await _seed_hero_outcome(db_session, signal=sig, hit_stop=True, max_adverse=None)

        body = (await client.get("/api/dashboard/stats")).json()
        assert body["last_stop_saved"] is None

    async def test_skips_stop_saved_with_zero_max_adverse(self, db_session, client):
        """`max_adverse = 0` means the trade hit the stop without ever moving
        underwater — framing this as "stop saved you 0%" would be absurd.
        Strict-reading gate (PR E.1 — backend stays honest, no zero-saves)."""
        sig = await _seed_signal_with_levels(db_session, key="zero-adv")
        await _seed_hero_outcome(db_session, signal=sig, hit_stop=True, max_adverse=Decimal("0"))

        body = (await client.get("/api/dashboard/stats")).json()
        assert body["last_stop_saved"] is None

    async def test_unevaluated_outcome_does_not_surface(self, db_session, client):
        """`evaluated_at IS NULL` means the evaluator hasn't completed yet —
        must not surface in either hero slot. Explicit WHERE clause in the
        query so a null doesn't sort ahead of a real value via Postgres
        default null-ordering."""
        sig_target = await _seed_signal_with_levels(db_session, key="uneval-t")
        sig_stop = await _seed_signal_with_levels(db_session, key="uneval-s")
        await _seed_hero_outcome(db_session, signal=sig_target, hit_target=True, evaluated_at=None)
        await _seed_hero_outcome(db_session, signal=sig_stop, hit_stop=True, evaluated_at=None)

        body = (await client.get("/api/dashboard/stats")).json()
        assert body["last_target_hit"] is None
        assert body["last_stop_saved"] is None

    async def test_headline_extracted_from_signal_ai_analysis(self, db_session, client):
        """Headline comes from `signal.ai_analysis["headline"]` (JSONB →
        dict in Python). Surfacing this means the FE doesn't need a second
        fetch to /signals/:id to render the card text."""
        sig = await _seed_signal_with_levels(
            db_session,
            key="headline",
            headline="BTC ETF outflows snap streak",
        )
        await _seed_hero_outcome(db_session, signal=sig, hit_target=True)

        body = (await client.get("/api/dashboard/stats")).json()
        assert body["last_target_hit"]["headline"] == "BTC ETF outflows snap streak"

    async def test_handles_signal_with_null_ai_analysis(self, db_session, client):
        """Defensive — `_build_hero_outcome` guards `signal.ai_analysis` with
        `isinstance(..., dict)`. In practice hit_target requires AI success
        (which produces non-null ai_analysis), so this path is unreachable
        in prod. The test pins the defensive contract so a future refactor
        that drops the guard breaks loudly."""
        sig = Signal(
            signal_type="flow_anomaly",
            asset="BTC",
            trigger_data={},
            ai_analysis=None,  # the defensive case
            confidence=7,
            status="alerted",
            price_at_creation=Decimal("84200"),
            price_source="binance",
            ai_prompt_version="v3",
            fingerprint=compute_fingerprint("hero-test", "null-ai"),
            signal_date=date(2026, 4, 25),
            entry_price=Decimal("84200"),
            stop_price=Decimal("82000"),
            target_price=Decimal("88000"),
        )
        db_session.add(sig)
        await db_session.flush()
        await _seed_hero_outcome(db_session, signal=sig, hit_target=True)

        body = (await client.get("/api/dashboard/stats")).json()
        hero = body["last_target_hit"]
        assert hero is not None  # outcome still surfaces — only headline is None
        assert hero["headline"] is None

    async def test_handles_non_dict_ai_analysis(self, db_session, client):
        """Defensive — `ai_analysis` is JSONB which CAN top-level a list or
        scalar. The `isinstance(..., dict)` guard catches that case and
        returns headline=None instead of raising on `.get("headline")`."""
        sig = await _seed_signal_with_levels(db_session, key="list-ai")
        sig.ai_analysis = ["not", "a", "dict"]  # type: ignore[assignment]
        await db_session.flush()
        await _seed_hero_outcome(db_session, signal=sig, hit_target=True)

        body = (await client.get("/api/dashboard/stats")).json()
        hero = body["last_target_hit"]
        assert hero is not None
        assert hero["headline"] is None

    async def test_handles_dict_ai_analysis_without_headline_key(self, db_session, client):
        """Defensive — dict shape but no `headline` key. The inner
        `isinstance(h, str)` guard returns None for missing/non-string."""
        sig = await _seed_signal_with_levels(db_session, key="no-headline-key")
        sig.ai_analysis = {"reasoning": ["x"], "confidence": 7}  # type: ignore[assignment]
        await db_session.flush()
        await _seed_hero_outcome(db_session, signal=sig, hit_target=True)

        body = (await client.get("/api/dashboard/stats")).json()
        hero = body["last_target_hit"]
        assert hero is not None
        assert hero["headline"] is None

    async def test_dto_carries_full_field_set(self, db_session, client):
        """Pins the wire shape — FE depends on every field listed here. A
        future schema edit that drops a field gets caught at the boundary
        instead of at runtime in the browser."""
        sig = await _seed_signal_with_levels(db_session, key="shape", asset="ETH")
        evaluated = datetime(2026, 5, 10, 12, 0, tzinfo=UTC)
        await _seed_hero_outcome(
            db_session,
            signal=sig,
            hit_target=True,
            max_favorable=Decimal("0.0735"),
            max_adverse=Decimal("0.0120"),
            evaluated_at=evaluated,
        )

        body = (await client.get("/api/dashboard/stats")).json()
        hero = body["last_target_hit"]
        assert set(hero.keys()) == {
            "signal_id",
            "asset",
            "signal_type",
            "direction",
            "confidence",
            "headline",
            "entry_price",
            "stop_price",
            "target_price",
            "price_at_signal",
            "max_favorable",
            "max_adverse",
            "evaluated_at",
            "signal_created_at",
        }
        assert hero["asset"] == "ETH"
        assert hero["direction"] == "long"
        assert hero["confidence"] == 7
        assert Decimal(hero["max_favorable"]) == Decimal("0.0735")
        assert Decimal(hero["max_adverse"]) == Decimal("0.0120")
