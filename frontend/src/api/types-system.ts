/**
 * TypeScript mirrors of the backend Pydantic DTOs — dashboard, prices, admin.
 *
 * Source of truth: etfpulse/backend/etfpulse/api/schemas/{dashboard,prices}.py
 * Update both when the API contract changes.
 */

import type { AssetSymbol, SignalType } from './types-signals';

// ---------------------------------------------------------------------------
// Dashboard
// ---------------------------------------------------------------------------

/** PR E.1 — single `SignalOutcome` surfaced on the home page hero card.
 *
 *  `max_favorable` and `max_adverse` are **unsigned fractions of entry**
 *  (e.g. `"0.05"` for 5%), matching the backend column semantics. The FE
 *  multiplies by 100 for display. Naming intentionally drops the `_pct`
 *  suffix — calling a fraction "pct" would mislead future readers.
 *
 *  Decimal-shaped fields arrive as strings over the wire (Pydantic default
 *  for Decimal). `Number(...)` at the rendering boundary; same pattern as
 *  the `Signal` price levels.
 *
 *  Keep this in sync with `etfpulse/backend/etfpulse/api/schemas/dashboard.py::HeroOutcome` —
 *  field-by-field; the response model is strict. */
export interface HeroOutcome {
  signal_id: number;
  asset: AssetSymbol;
  signal_type: SignalType;
  direction: 'long' | 'short';
  confidence: number;
  headline: string | null;
  entry_price: string;
  stop_price: string | null;
  target_price: string | null;
  price_at_signal: string;
  max_favorable: string | null;
  max_adverse: string | null;
  evaluated_at: string;
  signal_created_at: string;
}

export interface DashboardStats {
  total_signals: number;
  signals_today: number;
  /** null when no confidence-bearing signals exist (empty DB or all AI-failed). */
  avg_confidence: number | null;
  last_signal_at: string | null;
  /** Stage 7-P7 — latest regime classification surfaced on the home page so
   * the badge tile + TopNav indicator (#104) don't need a second
   * /api/regime roundtrip. Null when no classification exists yet (cold-boot
   * before the first daily cycle) OR when the latest snapshot is a legacy
   * pre-Stage-7 row with NULL regime columns. Field name `signal_posture`
   * matches /api/regime + the model column — one canonical name across
   * the stack. */
  current_regime: string | null;
  signal_posture: string | null;
  /** PR B (#60) — global hit rate as PERCENT (0..100). Renamed from
   *  `hit_rate_72h` because under the v2 rubric outcomes are scored
   *  against their OWN window, not a fixed 72h. Same unit as
   *  `/api/track-record.summary.hit_rate_pct`. Null when no signal with
   *  a target was scored — HeroHitRatePanel renders "—" + caption.
   *  Denominator is signals where AI set a target, NOT all evaluated
   *  signals. */
  hit_rate_global: number | null;
  /** DEPRECATED — backend writes the same value as `hit_rate_global` for
   *  one release cycle so a pinned-old frontend doesn't 422 on the
   *  response shape. Drop after the v2 frontend is the only consumer. */
  hit_rate_72h: number | null;
  /** Total SignalOutcome rows scored — captioned next to hit_rate_global
   *  ("on N evaluated signals"). 0 before any signal ages past its
   *  validity window. */
  evaluated_count: number;
  /** PR E.1 — hero card slots. Both null on cold-start or when no
   *  qualifying outcome exists. The `HeroOutcomeRow` component collapses
   *  the whole section when both are null — no hollow placeholder. */
  last_target_hit: HeroOutcome | null;
  last_stop_saved: HeroOutcome | null;
}

// ---------------------------------------------------------------------------
// Admin
// ---------------------------------------------------------------------------

export interface SignalStatusCounts {
  pending: number;
  alerted: number;
  expired: number;
}

export interface DeliveryStatusCounts {
  pending: number;
  delivered: number;
  failed: number;
  skipped: number;
}

export interface SchedulerJobInfo {
  id: string;
  /** ISO-8601 UTC datetime; null when the job is paused or not yet scheduled. */
  next_run_at: string | null;
  trigger: string;
  pending: boolean;
}

/** Response from `GET /api/prices/spot` — live BTC + ETH spot for the top nav. */
export interface SpotPrices {
  btc: number | null;
  eth: number | null;
  /** "sosovalue" | "binance" | "mixed" | null (both providers failed). */
  source: string | null;
  /** ISO-8601 UTC datetime — when the backend cache last refreshed. */
  fetched_at: string;
}

/** Response from `GET /api/admin/metrics` (admin-keyed). */
export interface AdminMetrics {
  signal_status_counts: SignalStatusCounts;
  delivery_status_counts: DeliveryStatusCounts;
  signals_overdue_unreaped: number;
  signals_null_confidence: number;
  deliveries_stuck_pending: number;
  deliveries_reaper_failures: number;
  /** Null when run_scheduler=false in the backend. */
  scheduler_jobs: SchedulerJobInfo[] | null;
  /** Null when bot disabled; >1 indicates a stuck rotation (#40). */
  accepted_webhook_secrets: number | null;
  /** Prompt version this process stamps on new signals (#32). */
  current_ai_prompt_version: string;
  /** Full-table distribution sorted by count DESC. */
  signal_counts_by_prompt_version: Record<string, number>;
}
