"""Demo data seeder — DESTRUCTIVE, dev-only.

Wipes and repopulates signals / outcomes / deliveries / users / groups so the
frontend has rich data to render. Idempotent by nuke-and-recreate: every run
clears the affected tables before reinserting. Safe to run repeatedly while
iterating on seed content — DO NOT run against production.

Run from backend/:

    uv run python scripts/seed_demo.py

What you get (12 signals total):
  - All 5 signal types × both assets, some duplicates for frontend variety
  - Confidence 2, 3, 4, 5, 6, 7, 8, 9, 10 (hits all ConfidenceBadge color buckets)
  - Two with ai_analysis=null (exercises the "AI analysis unavailable" branch
    on SignalCard + SignalDetail)
  - Two with SignalOutcome rows (display status → "evaluated")
  - One past expires_at (display status → "expired")
  - alerted_to varies 0–10 across rows (detail-page meta line varies)
  - created_at spread across the last 3 days so formatAgo() shows a range
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import delete

from etfpulse.db import async_session
from etfpulse.models import (
    NotificationChannel,
    Signal,
    SignalDelivery,
    SignalOutcome,
    TelegramGroup,
    User,
)

NOW = datetime.now(UTC)


def _fp(n: int) -> str:
    """Deterministic 32-char fingerprint so reseeds don't collide."""
    return f"demo{n:028d}"


def _ago(hours: float) -> datetime:
    return NOW - timedelta(hours=hours)


# ---------------------------------------------------------------------------
# Seed data — ordered chronologically (newest first in the feed after insert)
# ---------------------------------------------------------------------------

SIGNALS: list[dict] = [
    # 0 — Freshest, BTC flow_anomaly, high confidence, alerted
    {
        "signal_type": "flow_anomaly",
        "asset": "BTC",
        "confidence": 9,
        "status": "alerted",
        "created_at": _ago(0.3),
        "signal_date": _ago(0.3).date(),
        "expires_at": _ago(0.3) + timedelta(hours=72),
        "alerted_at": _ago(0.25),
        "trigger_data": {
            "streak_days": 5,
            "streak_direction": "inflow",
            "break_magnitude_usd": -78_400_000,
            "zscore": -2.41,
            "window_days": 30,
        },
        "ai_analysis": {
            "headline": "BTC inflows snap 5-day streak — sharp $78M reversal",
            "reasoning": [
                "Five consecutive inflow days abruptly reversed by a $78.4M outflow.",
                "The -2.4σ break magnitude is consistent with a single-fund de-risking move, "
                "not retail panic.",
                "Streak length of 5 days puts this in the 90th percentile for 2026 YTD.",
            ],
            "confidence": 9,
            "risks": [
                "Single-day reversal could be noise from quarter-end rebalancing.",
                "ETH flows remain positive — divergence may resolve by ETH weakening, "
                "not BTC strengthening short.",
            ],
            "suggested_action": "consider short",
            "time_horizon": "swing",
        },
        "alerted_to_count": 8,
    },
    # 1 — ETH magnitude, confidence 8, pending
    {
        "signal_type": "magnitude",
        "asset": "ETH",
        "confidence": 8,
        "status": "pending",
        "created_at": _ago(1.5),
        "signal_date": _ago(1.5).date(),
        "expires_at": _ago(1.5) + timedelta(hours=72),
        "alerted_at": None,
        "trigger_data": {
            "net_inflow_usd": 312_500_000,
            "zscore": 3.08,
            "window_days": 30,
            "top_contributor": "ETHA",
            "top_contributor_pct": 0.52,
        },
        "ai_analysis": {
            "headline": "ETH inflows breach $300M — largest single day of Q2",
            "reasoning": [
                "Single-day net inflow of $312.5M is 3.1σ above the 30-day mean.",
                "Over half of flow concentrated in ETHA, suggesting broker-advised rotation "
                "rather than basis trade.",
                "Comparable Q1 spikes preceded multi-week ETH rallies of 8-14%.",
            ],
            "confidence": 8,
            "risks": [
                "Outsized flows can mark local tops when paired with retail FOMO peaks.",
                "Macro CPI print in 48h may be front-running the spike.",
            ],
            "suggested_action": "consider long",
            "time_horizon": "position",
        },
        "alerted_to_count": 0,
    },
    # 2 — BTC acceleration, low confidence (wait) — exercises neg/warn territory
    {
        "signal_type": "acceleration",
        "asset": "BTC",
        "confidence": 4,
        "status": "alerted",
        "created_at": _ago(4),
        "signal_date": _ago(4).date(),
        "expires_at": _ago(4) + timedelta(hours=72),
        "alerted_at": _ago(3.9),
        "trigger_data": {
            "roc_3d_pct": 0.14,
            "roc_7d_pct": 0.03,
            "noise_band_pct": 0.12,
            "spot_price_divergence": True,
        },
        "ai_analysis": {
            "headline": "Flow acceleration ambiguous — wait for confirmation",
            "reasoning": [
                "3-day rate of change is positive but within the historical noise band.",
                "Underlying spot price action lags the flow signal — "
                "divergence reduces conviction.",
            ],
            "confidence": 4,
            "risks": [
                "Acting on weak acceleration signals historically underperforms net of fees.",
            ],
            "suggested_action": "wait",
            "time_horizon": "scalp",
        },
        "alerted_to_count": 3,
    },
    # 3 — ETH divergence, confidence 7, alerted
    {
        "signal_type": "divergence",
        "asset": "ETH",
        "confidence": 7,
        "status": "alerted",
        "created_at": _ago(8),
        "signal_date": _ago(8).date(),
        "expires_at": _ago(8) + timedelta(hours=72),
        "alerted_at": _ago(7.9),
        "trigger_data": {
            "flow_direction": "positive",
            "price_change_24h_pct": -0.027,
            "correlation_30d": 0.78,
            "correlation_7d": -0.21,
        },
        "ai_analysis": {
            "headline": "ETH price–flow divergence — flows up, spot down 2.7%",
            "reasoning": [
                "30-day correlation of 0.78 has collapsed to -0.21 over the last week.",
                "Institutional flows continue positive while spot is drifting lower — "
                "classic early-accumulation pattern.",
                "Similar divergences in 2025 resolved with spot catching up within 5-9 days.",
            ],
            "confidence": 7,
            "risks": [
                "Divergence can also resolve downward if flows turn.",
                "Macro risk-off episode could break the historical pattern.",
            ],
            "suggested_action": "consider long",
            "time_horizon": "swing",
        },
        "alerted_to_count": 10,
    },
    # 4 — BTC magnitude, confidence 6, pending, NULL ai_analysis (AI failed)
    {
        "signal_type": "magnitude",
        "asset": "BTC",
        "confidence": None,
        "status": "pending",
        "created_at": _ago(14),
        "signal_date": _ago(14).date(),
        "expires_at": None,
        "alerted_at": None,
        "trigger_data": {
            "net_inflow_usd": -425_000_000,
            "zscore": -2.88,
            "window_days": 30,
        },
        "ai_analysis": None,
        "alerted_to_count": 0,
    },
    # 5 — BTC flow_anomaly, confidence 6 (warn bucket), alerted
    {
        "signal_type": "flow_anomaly",
        "asset": "BTC",
        "confidence": 6,
        "status": "alerted",
        "created_at": _ago(22),
        "signal_date": _ago(22).date(),
        "expires_at": _ago(22) + timedelta(hours=72),
        "alerted_at": _ago(21.8),
        "trigger_data": {
            "streak_days": 3,
            "streak_direction": "outflow",
            "break_magnitude_usd": 42_000_000,
            "zscore": 1.6,
            "window_days": 30,
        },
        "ai_analysis": {
            "headline": "Mild BTC outflow streak ends — signal modest",
            "reasoning": [
                "Three-day outflow streak broken by a $42M inflow day.",
                "Break magnitude is 1.6σ — above noise but well below high-confidence thresholds.",
                "Supporting evidence from ETH flows is mixed.",
            ],
            "confidence": 6,
            "risks": [
                "Short streak lengths produce more false positives historically.",
            ],
            "suggested_action": "wait",
            "time_horizon": "scalp",
        },
        "alerted_to_count": 5,
    },
    # 6 — ETH regime_shift, confidence 10, alerted — top-tier signal, has outcome
    {
        "signal_type": "regime_shift",
        "asset": "ETH",
        "confidence": 10,
        "status": "alerted",
        "created_at": _ago(30),
        "signal_date": _ago(30).date(),
        "expires_at": _ago(30) + timedelta(hours=72),
        "alerted_at": _ago(29.8),
        "trigger_data": {
            "previous_regime": "risk_off",
            "new_regime": "risk_on",
            "btc_dominance": 0.513,
            "btc_dominance_delta_7d": -0.021,
            "altseason_index": 72,
        },
        "ai_analysis": {
            "headline": "Regime flip — ETH leads risk-on rotation as BTC dominance falls",
            "reasoning": [
                "BTC dominance dropped 2.1 percentage points over 7 days, breaking the 52% floor.",
                "Altseason index crossed 70 for the first time in 2026.",
                "ETH ETF flows accelerated while BTC flows flattened — textbook rotation pattern.",
            ],
            "confidence": 10,
            "risks": [
                "Regime shifts are notoriously hard to time entries on.",
                "A macro risk-off shock (geopolitical, liquidity) would unwind the shift rapidly.",
            ],
            "suggested_action": "consider long",
            "time_horizon": "position",
        },
        "alerted_to_count": 12,
        "outcome": {
            "direction": "long",
            "price_at_signal": Decimal("2480.50"),
            "price_after_24h": Decimal("2521.10"),
            "price_after_72h": Decimal("2612.75"),
            "hit_target": True,
            "hit_stop": False,
            "evaluated_at": _ago(30) + timedelta(hours=72),
        },
    },
    # 7 — BTC divergence, confidence 5 (warn), alerted
    {
        "signal_type": "divergence",
        "asset": "BTC",
        "confidence": 5,
        "status": "alerted",
        "created_at": _ago(42),
        "signal_date": _ago(42).date(),
        "expires_at": _ago(42) + timedelta(hours=72),
        "alerted_at": _ago(41.8),
        "trigger_data": {
            "flow_direction": "negative",
            "price_change_24h_pct": 0.018,
            "correlation_30d": 0.81,
            "correlation_7d": 0.12,
        },
        "ai_analysis": {
            "headline": "BTC flow/price divergence weakens but persists",
            "reasoning": [
                "Spot up 1.8% while ETF flows net negative for the third consecutive day.",
                "Correlation breakdown less severe than last week's signal "
                "but directionally intact.",
            ],
            "confidence": 5,
            "risks": [
                "Divergence trades require patience; stop-outs common on 1-2 day timeframes.",
            ],
            "suggested_action": "wait",
            "time_horizon": "swing",
        },
        "alerted_to_count": 4,
    },
    # 8 — BTC acceleration, confidence 3 (neg bucket), alerted, evaluated (missed)
    {
        "signal_type": "acceleration",
        "asset": "BTC",
        "confidence": 3,
        "status": "alerted",
        "created_at": _ago(58),
        "signal_date": _ago(58).date(),
        "expires_at": _ago(58) + timedelta(hours=72),
        "alerted_at": _ago(57.8),
        "trigger_data": {
            "roc_3d_pct": -0.09,
            "roc_7d_pct": -0.02,
            "noise_band_pct": 0.12,
        },
        "ai_analysis": {
            "headline": "Weak BTC flow deceleration — low conviction",
            "reasoning": [
                "ROC within noise band; no statistically clean acceleration break.",
                "Published anyway for track-record transparency — "
                "low-confidence signals still evaluated.",
            ],
            "confidence": 3,
            "risks": [
                "Low-confidence signals have a ~40% historical hit rate; "
                "position sizing should reflect that.",
            ],
            "suggested_action": "wait",
            "time_horizon": "scalp",
        },
        "alerted_to_count": 2,
        "outcome": {
            "direction": "neutral",
            "price_at_signal": Decimal("84120.50"),
            "price_after_24h": Decimal("83980.00"),
            "price_after_72h": Decimal("84051.25"),
            "hit_target": False,
            "hit_stop": False,
            "evaluated_at": _ago(58) + timedelta(hours=72),
        },
    },
    # 9 — ETH flow_anomaly, confidence 8 (pos), alerted, NULL ai_analysis (AI failed for 2nd time)
    {
        "signal_type": "flow_anomaly",
        "asset": "ETH",
        "confidence": None,
        "status": "alerted",
        "created_at": _ago(66),
        "signal_date": _ago(66).date(),
        "expires_at": _ago(66) + timedelta(hours=72),
        "alerted_at": _ago(65.8),
        "trigger_data": {
            "streak_days": 6,
            "streak_direction": "inflow",
            "break_magnitude_usd": -95_000_000,
            "zscore": -2.9,
            "window_days": 30,
        },
        "ai_analysis": None,
        "alerted_to_count": 0,
    },
    # 10 — ETH magnitude, confidence 2 (neg bucket), pending
    {
        "signal_type": "magnitude",
        "asset": "ETH",
        "confidence": 2,
        "status": "pending",
        "created_at": _ago(70),
        "signal_date": _ago(70).date(),
        "expires_at": _ago(70) + timedelta(hours=72),
        "alerted_at": None,
        "trigger_data": {
            "net_inflow_usd": 48_000_000,
            "zscore": 0.8,
            "window_days": 30,
        },
        "ai_analysis": {
            "headline": "ETH inflows present but below statistical threshold",
            "reasoning": [
                "Below 1σ from the mean — magnitude does not warrant directional bias.",
                "Published as a regular cadence signal so track-record reflects all evaluations.",
            ],
            "confidence": 2,
            "risks": [
                "Ignore-this-signal-on-its-own risk: confidence floor in delivery prefs "
                "exists for a reason.",
            ],
            "suggested_action": "wait",
            "time_horizon": "scalp",
        },
        "alerted_to_count": 0,
    },
    # 11 — Oldest, BTC regime_shift, confidence 7, EXPIRED (past expires_at)
    {
        "signal_type": "regime_shift",
        "asset": "BTC",
        "confidence": 7,
        "status": "alerted",
        "created_at": _ago(80),  # created 80h ago
        "signal_date": _ago(80).date(),
        "expires_at": _ago(80) + timedelta(hours=72),  # expired 8h ago
        "alerted_at": _ago(79.8),
        "trigger_data": {
            "previous_regime": "risk_on",
            "new_regime": "consolidation",
            "btc_dominance": 0.541,
            "btc_dominance_delta_7d": 0.008,
        },
        "ai_analysis": {
            "headline": "BTC regime consolidation — rotation pause signalled",
            "reasoning": [
                "BTC dominance stabilised after three weeks of decline.",
                "Flow data shows reduced conviction across both assets simultaneously.",
            ],
            "confidence": 7,
            "risks": [
                "Consolidation regimes often break back toward the prior trend.",
            ],
            "suggested_action": "wait",
            "time_horizon": "position",
        },
        "alerted_to_count": 6,
    },
]


async def main() -> None:
    async with async_session() as s:
        # Wipe in FK-reverse order. Dev-only; trashes any existing rows.
        await s.execute(delete(SignalDelivery))
        await s.execute(delete(SignalOutcome))
        await s.execute(delete(Signal))
        await s.execute(delete(NotificationChannel))
        await s.execute(delete(User))
        await s.execute(delete(TelegramGroup))
        await s.flush()

        # Pool of demo recipients so alerted_to_count can vary up to 12.
        # The partial unique indexes ux_delivery_user_signal + ux_delivery_group_signal
        # allow one row per (signal, user) and per (signal, group), so we need
        # N distinct recipients to hit alerted_to=N on any signal.
        users: list[User] = []
        channels: list[NotificationChannel] = []
        for u in range(12):
            user = User(
                role="user",
                tier="free",
                pref_assets=["BTC", "ETH"],
                pref_min_confidence=5,
                preferences={"demo": True, "seed_idx": u},
            )
            s.add(user)
            users.append(user)
        await s.flush()

        for u, user in enumerate(users):
            ch = NotificationChannel(
                user_id=user.id,
                channel_type="telegram",
                channel_identifier=f"demo-chat-{u:03d}",
                username=f"demo_user_{u:02d}",
            )
            s.add(ch)
            channels.append(ch)

        groups: list[TelegramGroup] = []
        for g in range(2):
            group = TelegramGroup(
                chat_id=-1001234567000 - g,
                title=f"ETFPulse Demo Group {g + 1}",
                pref_assets=["BTC", "ETH"],
                pref_min_confidence=6,
                preferences={"demo": True, "seed_idx": g},
            )
            s.add(group)
            groups.append(group)
        await s.flush()

        # Signals, outcomes, deliveries
        for i, spec in enumerate(SIGNALS):
            outcome_spec = spec.pop("outcome", None)
            alerted_to_count = spec.pop("alerted_to_count", 0)

            sig = Signal(fingerprint=_fp(i), **spec)
            s.add(sig)
            await s.flush()

            if outcome_spec:
                outcome = SignalOutcome(
                    signal_id=sig.id,
                    asset=sig.asset,
                    signal_type=sig.signal_type,
                    confidence=sig.confidence or 5,
                    **outcome_spec,
                )
                s.add(outcome)

            # Fan out to the first N distinct recipients — mix users + groups.
            # Prefer users; fall back to groups if count exceeds user pool.
            recipients = [("user", u.id, c.id) for u, c in zip(users, channels, strict=True)]
            recipients += [("group", g.id, None) for g in groups]

            for kind, target_id, channel_id in recipients[:alerted_to_count]:
                delivered_at = sig.alerted_at or sig.created_at
                if kind == "user":
                    s.add(
                        SignalDelivery(
                            signal_id=sig.id,
                            user_id=target_id,
                            channel_id=channel_id,
                            status="delivered",
                            delivered_at=delivered_at,
                        )
                    )
                else:
                    s.add(
                        SignalDelivery(
                            signal_id=sig.id,
                            group_id=target_id,
                            status="delivered",
                            delivered_at=delivered_at,
                        )
                    )

        await s.commit()

    print(f"Seeded {len(SIGNALS)} signals + 2 outcomes + deliveries.")


if __name__ == "__main__":
    asyncio.run(main())
