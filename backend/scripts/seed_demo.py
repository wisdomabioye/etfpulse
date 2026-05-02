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
  - Stage 7-P10: a subset of signals carries `regime_at_creation` +
    `news_context` in trigger_data and `ai_prompt_version="v2"` so the
    web News Context section + Telegram regime block + the regime tile
    on the home page all render with realistic data. The two regime_shift
    signals use the live Wyckoff enum values (accumulation/markup/etc.),
    not the pre-Stage-7 risk_on/risk_off/consolidation strings.
  - One `RegimeSnapshot` row so `/regime` returns 200 and the TopNav badge
    + home tile light up. Three `NewsItem` rows so the news ingestion
    surface looks live (also feeds the news_context query in dev).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import delete

from etfpulse.db import async_session
from etfpulse.models import (
    MarketRegime,
    NewsCategory,
    NewsItem,
    NotificationChannel,
    RegimeSnapshot,
    Signal,
    SignalDelivery,
    SignalOutcome,
    SignalPosture,
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

# Reusable Stage 7-P10 trigger-data fragments. Defined once so the regime
# context is consistent across the signals that carry it (mirrors how
# real signals built in the same daily cycle would all share the same
# `regime_at_creation` snapshot).
_REGIME_DISTRIBUTION_CAUTIOUS = {
    "regime": MarketRegime.DISTRIBUTION.value,
    "signal_posture": SignalPosture.CAUTIOUS.value,
    "confidence": 7,
    "macro_events_nearby": ["FOMC meeting Apr 30"],
}
_REGIME_MARKUP_NORMAL = {
    "regime": MarketRegime.MARKUP.value,
    "signal_posture": SignalPosture.NORMAL.value,
    "confidence": 8,
    "macro_events_nearby": [],
}
_REGIME_ACCUMULATION_AGGRESSIVE = {
    "regime": MarketRegime.ACCUMULATION.value,
    "signal_posture": SignalPosture.AGGRESSIVE.value,
    "confidence": 6,
    "macro_events_nearby": [],
}

_NEWS_CONTEXT_BTC_INFLOWS = [
    {
        "title": "BTC ETF inflows turn negative for the first time in 5 days",
        "summary": (
            "Spot BTC ETFs saw a $78M net outflow led by FBTC redemptions, "
            "ending a multi-day streak."
        ),
        "category": int(NewsCategory.NEWS),
        "published_iso": _ago(2).isoformat(),
    },
    {
        "title": "BlackRock files for in-kind ETF redemptions",
        "summary": (
            "An SEC filing seeks approval for in-kind creates/redeems on IBIT "
            "— would reduce slippage on rotations."
        ),
        "category": int(NewsCategory.INSTITUTION),
        "published_iso": _ago(5).isoformat(),
    },
]

_NEWS_CONTEXT_ETH_FLOWS = [
    {
        "title": "ETH ETF flows hit Q2 high — $312M in single day",
        "summary": (
            "ETHA captured 52% of the day's inflow, suggesting broker-advised "
            "rotation rather than basis trade."
        ),
        "category": int(NewsCategory.NEWS),
        "published_iso": _ago(3).isoformat(),
    },
    {
        "title": "Macro CPI print due in 48 hours",
        "summary": (
            "Consensus forecasts a 0.2% MoM core CPI; surprise to the upside "
            "would headwind risk assets."
        ),
        "category": int(NewsCategory.RESEARCH),
        "published_iso": _ago(8).isoformat(),
    },
]


SIGNALS: list[dict] = [
    # 0 — Freshest, BTC flow_anomaly, high confidence, alerted.
    # Stage 7-P10: carries regime_at_creation + news_context + ai_prompt_version
    # so the web News Context section + Telegram regime block render with
    # realistic data.
    {
        "signal_type": "flow_anomaly",
        "asset": "BTC",
        "confidence": 9,
        "status": "alerted",
        "created_at": _ago(0.3),
        "signal_date": _ago(0.3).date(),
        "expires_at": _ago(0.3) + timedelta(hours=72),
        "alerted_at": _ago(0.25),
        "ai_prompt_version": "v2",
        "trigger_data": {
            "streak_days": 5,
            "streak_direction": "inflow",
            "break_magnitude_usd": -78_400_000,
            "zscore": -2.41,
            "window_days": 30,
            "regime_at_creation": _REGIME_DISTRIBUTION_CAUTIOUS,
            "news_context": _NEWS_CONTEXT_BTC_INFLOWS,
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
    # 1 — ETH magnitude, confidence 8, pending. Stage 7-P10: carries
    # markup-regime context + the matching news set.
    {
        "signal_type": "magnitude",
        "asset": "ETH",
        "confidence": 8,
        "status": "pending",
        "created_at": _ago(1.5),
        "signal_date": _ago(1.5).date(),
        "expires_at": _ago(1.5) + timedelta(hours=72),
        "alerted_at": None,
        "ai_prompt_version": "v2",
        "trigger_data": {
            "net_inflow_usd": 312_500_000,
            "zscore": 3.08,
            "window_days": 30,
            "top_contributor": "ETHA",
            "top_contributor_pct": 0.52,
            "regime_at_creation": _REGIME_MARKUP_NORMAL,
            "news_context": _NEWS_CONTEXT_ETH_FLOWS,
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
    # 6 — ETH regime_shift, confidence 10, alerted — top-tier signal, has outcome.
    # Stage 7-P10: trigger_data uses the live Wyckoff enum values
    # (`previous_regime`/`new_regime` are MarketRegime members), not the
    # pre-Stage-7 risk_on/risk_off strings. Carries the matching
    # regime_at_creation snapshot + an accumulation-flavored news context.
    {
        "signal_type": "regime_shift",
        "asset": "ETH",
        "confidence": 10,
        "status": "alerted",
        "created_at": _ago(30),
        "signal_date": _ago(30).date(),
        "expires_at": _ago(30) + timedelta(hours=72),
        "alerted_at": _ago(29.8),
        "ai_prompt_version": "v2",
        "trigger_data": {
            "previous_regime": MarketRegime.UNCERTAIN.value,
            "new_regime": MarketRegime.MARKUP.value,
            "score": 38,
            "btc_dominance": 0.513,
            "btc_dominance_delta_7d": -0.021,
            "regime_at_creation": _REGIME_MARKUP_NORMAL,
            "news_context": _NEWS_CONTEXT_ETH_FLOWS,
        },
        "ai_analysis": {
            "headline": "Regime flip — ETH leads markup phase as BTC dominance falls",
            "reasoning": [
                "Composite score crossed +25 — uncertain → markup transition confirmed.",
                "BTC dominance dropped 2.1 percentage points over 7 days, breaking the 52% floor.",
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
    # 11 — Oldest, BTC regime_shift, confidence 7, EXPIRED (past expires_at).
    # Stage 7-P10: live Wyckoff transition (markup → distribution) +
    # accumulation-window regime context.
    {
        "signal_type": "regime_shift",
        "asset": "BTC",
        "confidence": 7,
        "status": "alerted",
        "created_at": _ago(80),  # created 80h ago
        "signal_date": _ago(80).date(),
        "expires_at": _ago(80) + timedelta(hours=72),  # expired 8h ago
        "alerted_at": _ago(79.8),
        "ai_prompt_version": "v2",
        "trigger_data": {
            "previous_regime": MarketRegime.MARKUP.value,
            "new_regime": MarketRegime.DISTRIBUTION.value,
            "score": -22,
            "btc_dominance": 0.541,
            "btc_dominance_delta_7d": 0.008,
            "regime_at_creation": _REGIME_ACCUMULATION_AGGRESSIVE,
            "news_context": _NEWS_CONTEXT_BTC_INFLOWS,
        },
        "ai_analysis": {
            "headline": "BTC distribution phase — markup rotation losing steam",
            "reasoning": [
                "Composite score flipped to -22, breaching the markup → distribution threshold.",
                "BTC dominance stabilised after three weeks of decline.",
                "Flow data shows reduced conviction across both assets simultaneously.",
            ],
            "confidence": 7,
            "risks": [
                "Distribution can resolve back to markup if macro liquidity reaccelerates.",
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
        # RegimeSnapshot + NewsItem have no FKs into anything else, so
        # ordering with the rest doesn't matter, but include them in the
        # wipe so reseeds are clean.
        await s.execute(delete(SignalDelivery))
        await s.execute(delete(SignalOutcome))
        await s.execute(delete(Signal))
        await s.execute(delete(NotificationChannel))
        await s.execute(delete(User))
        await s.execute(delete(TelegramGroup))
        await s.execute(delete(RegimeSnapshot))
        await s.execute(delete(NewsItem))
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

        # Stage 7-P10 — RegimeSnapshot + NewsItem.
        # `/api/regime` returns 503 ("regime not yet classified") until at
        # least one row exists with non-NULL regime/posture/confidence, so
        # without this seed the dedicated /regime page + the TopNav badge
        # both render empty in dev. Single recent snapshot — multiple would
        # be more realistic but `get_latest_regime` only reads the newest,
        # so one is sufficient for the visual surface.
        s.add(
            RegimeSnapshot(
                captured_at=_ago(0.5),
                regime=MarketRegime.DISTRIBUTION.value,
                signal_posture=SignalPosture.CAUTIOUS.value,
                confidence=7,
                btc_dominance=Decimal("0.541"),
                flow_trend_7d=Decimal("-659516761.40"),
                news_velocity=40,
                # Score breakdown shape mirrors `pipeline/regime_monitor.classify`
                # so the /regime page's "Score breakdown" cards render with
                # realistic numbers. `dominance` carries the
                # sector-spotlight-pending note (see open issue #25).
                reasoning={
                    "score": -11,
                    "flow": {
                        "window_days": 7,
                        "by_asset_usd": {
                            "BTC": "-475867164.685",
                            "ETH": "-183649596.720",
                        },
                        "combined_usd": "-659516761.405",
                        "score": -6,
                    },
                    "news": {
                        "window_hours": 24,
                        "velocity_count": 40,
                        "score": -5,
                    },
                    "macro": {
                        "window_days": 2,
                        "events_nearby": ["FOMC meeting Apr 30"],
                    },
                    "dominance": {
                        "available": False,
                        "note": "sector-spotlight endpoint pending — see open issue #25",
                    },
                },
                macro_events={"events_nearby": ["FOMC meeting Apr 30"]},
            )
        )

        # Three NewsItem rows so the news ingestion surface looks live in
        # dev. The `currencies` JSONB matches the GIN-indexed containment
        # query shape (`@> [{"symbol": "BTC"}]`) used by gather_news_context,
        # so a fresh signal built against this seed data would actually
        # find news to thread into trigger_data.news_context.
        for i, (cat, title, summary, currencies, hours_ago) in enumerate(
            [
                (
                    NewsCategory.NEWS,
                    "BTC ETF inflows turn negative",
                    "Spot BTC ETFs saw a $78M net outflow led by FBTC redemptions.",
                    [{"symbol": "BTC", "name": "Bitcoin"}],
                    2,
                ),
                (
                    NewsCategory.INSTITUTION,
                    "BlackRock files for in-kind ETF redemptions",
                    "Filing seeks SEC approval for in-kind creates/redeems on IBIT.",
                    [{"symbol": "BTC", "name": "Bitcoin"}],
                    5,
                ),
                (
                    NewsCategory.NEWS,
                    "ETH ETF flows hit Q2 high — $312M in single day",
                    "ETHA captured 52% of the day's inflow.",
                    [{"symbol": "ETH", "name": "Ethereum"}],
                    3,
                ),
            ]
        ):
            s.add(
                NewsItem(
                    source_id=f"demo-news-{i:04d}",
                    category=int(cat),
                    captured_at=_ago(hours_ago),
                    title=title,
                    content_summary=summary,
                    currencies=currencies,
                )
            )

        await s.commit()

    print(
        f"Seeded {len(SIGNALS)} signals + 2 outcomes + deliveries "
        "+ 1 regime snapshot + 3 news items."
    )


if __name__ == "__main__":
    asyncio.run(main())
