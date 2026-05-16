/**
 * TypeScript mirrors of the backend Pydantic DTOs.
 *
 * Source of truth: etfpulse/backend/etfpulse/api/schemas/{signals,dashboard}.py
 * Update both when the API contract changes. Auto-generation from OpenAPI is
 * a future improvement; for ~4 endpoints with stable shapes the manual mirror
 * is faster to read in code review than a generated blob.
 */

// ---------------------------------------------------------------------------
// Enums (Literal in Pydantic, union types here)
// ---------------------------------------------------------------------------

// Issue #75 — these unions are derived from the runtime constant arrays in
// `lib/constants.ts`. Re-imported + re-exported here so the established
// `import type { AssetSymbol, SignalType } from '../../api/types'` path
// keeps working for every consumer, AND the in-file interfaces below can
// still reference the local names.
import type { AssetSymbol, SignalType } from '../lib/constants';
export type { AssetSymbol, SignalType };

/** Display status — derived backend-side; never the raw DB enum value.
 * - 'evaluated' = Stage 8 outcome row exists
 * - 'expired'   = past expires_at (overrides raw 'alerted')
 * - 'alerted'   = fan-out completed, awaiting outcome
 * - 'pending'   = signal built, fan-out not yet run
 */
export type DisplayStatus = 'pending' | 'alerted' | 'evaluated' | 'expired';

export type SuggestedAction = 'consider long' | 'consider short' | 'wait';

export type TimeHorizon = 'scalp' | 'swing' | 'position';

// ---------------------------------------------------------------------------
// Signal shapes
// ---------------------------------------------------------------------------

export interface SignalListItem {
  id: number;
  asset: AssetSymbol;
  signal_type: SignalType;
  status: DisplayStatus;
  confidence: number | null;

  /** Flattened from ai_analysis; null when AI failed at signal-build time. */
  headline: string | null;
  suggested_action: SuggestedAction | null;
  time_horizon: TimeHorizon | null;

  /** ISO date YYYY-MM-DD (no time component). */
  signal_date: string;
  /** ISO datetime with Z suffix. */
  created_at: string;
  expires_at: string | null;

  /** Count of SignalDelivery rows — "attempted" semantics. */
  alerted_to: number;
}

export interface AIAnalysis {
  headline: string;
  reasoning: string[];
  confidence: number;
  risks: string[];
  suggested_action: SuggestedAction;
  time_horizon: TimeHorizon;
  /** Stage 8-P1 — AI-suggested price levels. All three are null when
   *  `suggested_action === 'wait'` (validator drops them) OR when the AI
   *  declined to volunteer specific levels. The same values are mirrored
   *  onto `Signal.entry_price/stop_price/target_price` columns server-side
   *  for the outcome evaluator; the API exposes them here on `ai_analysis`
   *  to keep the "what the AI said" shape natural for the frontend. */
  entry_price: number | null;
  stop_price: number | null;
  target_price: number | null;
}

/** PR B (#60) — horizon bucket label. Mirrors backend
 *  `pipeline.track_record.HorizonLabel`. `legacy` is the bucket for rows
 *  written before PR B's v2 rubric (NULL `scoring_version`); v2 rows
 *  bucket by their AI-stated time horizon. The bucket key — not the
 *  raw `window_hours` — is what the UI cares about. */
export type HorizonLabel = 'scalp' | 'swing' | 'position' | 'legacy';

export interface SignalOutcome {
  entry_price: number | null;
  stop_price: number | null;
  target_price: number | null;
  price_at_signal: number;
  price_after_24h: number | null;
  price_after_72h: number | null;
  /** PR B (#60) — close at t0 + window_hours (the AI-stated validity end).
   *  Canonical "outcome price" for v2 rows; falls back to `price_after_72h`
   *  for legacy rows (NULL `scoring_version`). For swing signals (72h
   *  window) this equals `price_after_72h` by construction; for position
   *  (168h) it's the close at 7 days, beyond the legacy 72h checkpoint;
   *  for scalp (#62 pending intraday klines) it stays null. */
  price_at_validity_end: number | null;
  /** PR B (#60) — the scoring window used for this outcome, in hours.
   *  Derived at evaluation time from `(Signal.expires_at - Signal.created_at)`.
   *  NULL on legacy rows (pre-PR-B; scored against fixed 72h). FE uses
   *  this to pick the right "+Xh" label on the outcome card (scalp 6h /
   *  swing 24h+72h / position 24h+72h+168h). */
  window_hours: number | null;
  /** PR B (#60) — rubric version that produced this row. `'v2'` for
   *  outcomes written by the per-horizon evaluator; NULL on legacy rows
   *  (treated as `'v1'` by the reader). FE renders a "legacy 72h
   *  scoring" badge when NULL — flagged for the wipe-and-reevaluate
   *  cleanup tracked as #61. */
  scoring_version: string | null;
  hit_target: boolean | null;
  hit_stop: boolean | null;
  max_favorable: number | null;
  max_adverse: number | null;
  evaluated_at: string | null;
}

export interface SignalDetail {
  id: number;
  asset: AssetSymbol;
  signal_type: SignalType;
  status: DisplayStatus;
  confidence: number | null;
  /** Full 32-char SHA-256 prefix; truncate client-side for display. */
  fingerprint: string;
  signal_date: string;
  created_at: string;
  expires_at: string | null;
  alerted_to: number;

  trigger_data: Record<string, unknown>;
  ai_analysis: AIAnalysis | null;
  outcome: SignalOutcome | null;

  /** Live spot price (USD) captured at signal-build time + provenance of
   *  the fetch (`sosovalue` primary, `binance` fallback). Surfaced on the
   *  detail page so traders can anchor entry/stop/target against the actual
   *  market price when the signal fired — without waiting the 24-72h until
   *  the outcome row exists. NULL when both providers failed at build time
   *  (the backend backfill script revisits these rows; once filled the
   *  field populates retroactively). Mirrors `SignalDetail` on the
   *  backend — keep field names + nullability in sync. */
  price_at_creation: number | null;
  price_source: string | null;
}

export interface PaginatedSignals {
  items: SignalListItem[];
  /** Cursor pagination — pass back as `?cursor=` to fetch the next page. */
  next_cursor: string | null;
  /** Total rows matching the current filter set (page mode + cursor mode). */
  total: number;
  /** 1-based current page number, or `null` in cursor mode (no page concept). */
  page: number | null;
  /** ceil(total / limit). 0 when result set is empty. */
  total_pages: number;
}

// ---------------------------------------------------------------------------
// Filters (query params for /api/signals)
// ---------------------------------------------------------------------------

export type SortOrder = 'newest' | 'oldest';

export interface SignalFilters {
  asset?: AssetSymbol;
  signal_type?: SignalType;
  confidence_min?: number;
  include_expired?: boolean;
  sort?: SortOrder;
  limit?: number;
  /** 1-based page number for offset pagination. Omit to use cursor mode. */
  page?: number;
}

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
// Regime
// ---------------------------------------------------------------------------

/** Wyckoff-style market phase. Mirrors `MarketRegime` StrEnum on the backend. */
export type MarketRegime =
  | 'accumulation'
  | 'markup'
  | 'distribution'
  | 'markdown'
  | 'uncertain';

/** How aggressively to fire signals given the regime + macro context. */
export type SignalPosture = 'aggressive' | 'normal' | 'cautious' | 'paused';

// ---------------------------------------------------------------------------
// Track record (Stage 8-P4)
// ---------------------------------------------------------------------------

/** Aggregate stats over the SAME filter set as the paginated `items` —
 *  see `api/routes/track_record.py` for why summary mirrors filters
 *  (the dashboard endpoint carries the global number). All counts run
 *  over evaluated SignalOutcome rows.
 *
 *  `hit_rate_pct` divides `targets_hit / targeted_count`, NOT
 *  `targets_hit / total_evaluated` — signals where AI declined a target
 *  shouldn't dilute the rate. Same rationale as the dashboard's
 *  `hit_rate_72h`. */
export interface TrackRecordSummary {
  total_evaluated: number;
  targets_hit: number;
  stops_hit: number;
  /** Subset of `total_evaluated` where the AI set a target — denominator
   *  for `hit_rate_pct`. */
  targeted_count: number;
  /** 0..100. Null when `targeted_count === 0`. */
  hit_rate_pct: number | null;
  /** PR B (#60) — bucketed hit rate. Keys are `'scalp' | 'swing' |
   *  'position' | 'legacy'` (all four always present). Each value is
   *  null when that bucket has no targeted signals — same null-vs-zero
   *  convention as `hit_rate_pct`. Drives the side-by-side bucket tiles
   *  on the TrackRecord page; intentionally computed across ALL rows
   *  regardless of the `horizon` filter so the comparison stays
   *  apples-to-apples. */
  hit_rate_by_horizon: Record<HorizonLabel, number | null>;
  avg_confidence_hits: number | null;
  avg_confidence_misses: number | null;
}

/** One row in `PaginatedTrackRecord.items`. Mirrors `TrackRecordItemOut`
 *  on the backend — denormalized from Signal so each row is renderable
 *  on its own without a JOIN. */
export interface TrackRecordItem {
  id: number;
  signal_id: number;
  asset: AssetSymbol;
  signal_type: SignalType;
  direction: string;
  confidence: number;

  entry_price: number | null;
  stop_price: number | null;
  target_price: number | null;
  price_at_signal: number;
  price_after_24h: number | null;
  price_after_72h: number | null;
  /** PR B (#60) — see `SignalOutcome` for field semantics; same three
   *  fields, same NULL-means-legacy convention. */
  price_at_validity_end: number | null;
  window_hours: number | null;
  scoring_version: string | null;

  /** Tri-state — `true` hit, `false` did not hit, `null` no level set. */
  hit_target: boolean | null;
  hit_stop: boolean | null;

  /** Unsigned fractions of entry (0.032 = 3.2%). */
  max_favorable: number | null;
  max_adverse: number | null;

  evaluated_at: string;
}

export interface PaginatedTrackRecord {
  summary: TrackRecordSummary;
  items: TrackRecordItem[];
  next_cursor: string | null;
  total: number;
  page: number | null;
  total_pages: number;
}

/** Query params for `GET /api/track-record`. Subset of `SignalFilters`
 *  (no `sort` / `include_expired`) — the track-record endpoint sorts
 *  fixed by `evaluated_at DESC` and only ever returns evaluated rows. */
export interface TrackRecordFilters {
  asset?: AssetSymbol;
  signal_type?: SignalType;
  confidence_min?: number;
  /** PR B (#60) — restrict to one horizon bucket. Omit for all rows
   *  (including grandfathered `legacy`). Mirrors the backend
   *  `?horizon=` query param. The bucketed `summary.hit_rate_by_horizon`
   *  is ALWAYS all four buckets regardless — only the paginated `items`
   *  and flat aggregate counts respect the filter. */
  horizon?: HorizonLabel;
  limit?: number;
  /** 1-based page number for offset pagination. Omit to use cursor mode. */
  page?: number;
}

// ---------------------------------------------------------------------------
// Regime
// ---------------------------------------------------------------------------

/** Response from `GET /api/regime`. The `reasoning` JSONB is pass-through —
 * see `pipeline/regime_monitor.py` for the structured score-breakdown shape
 * the classifier writes. Consumers must tolerate missing top-level keys.
 *
 * Endpoint returns 503 when the table is empty or the latest snapshot is a
 * legacy pre-Stage-7 row — frontend treats fetch errors as "no regime yet"
 * rather than rendering hollow card. */
export interface RegimeResponse {
  regime: MarketRegime;
  signal_posture: SignalPosture;
  confidence: number;
  reasoning: Record<string, unknown>;
  macro_events_nearby: string[];
  classified_at: string;
}

// ---------------------------------------------------------------------------
// Analytics — backs `/analytics` page (Stage 8-P10).
// Mirrors backend `api/schemas/analytics.py` exactly. Field-for-field rename
// in either layer breaks the FE; keep the two in sync in the same change.
// ---------------------------------------------------------------------------

/** One row of a categorical breakdown (detector / asset / direction /
 *  confidence-bucket). `hit_rate_pct` is null when `targeted === 0` — the
 *  null-vs-zero distinction is load-bearing; render null as "—" or "pending",
 *  never as "0%". */
export interface BreakdownStat {
  label: string;
  total: number;
  /** Subset of `total` where the source signal had a target_price (i.e. AI
   *  set a target). Denominator for `hit_rate_pct`. */
  targeted: number;
  hits: number;
  /** 0..100, rounded to 2dp by `compute_hit_rate_pct` on the backend. */
  hit_rate_pct: number | null;
}

/** One bin of the MFE/MAE histogram. `lower` inclusive, `upper` exclusive.
 *  The final bucket has `upper: null` (open-ended ≥10%). Both bounds are
 *  unsigned fractions — 0.025 means 2.5%. */
export interface HistogramBucket {
  label: string;
  lower: number;
  upper: number | null;
  count: number;
}

/** Response from `GET /api/analytics/breakdown` (public).
 *
 *  All four categorical breakdowns share the `BreakdownStat[]` shape so the
 *  frontend renders them with a single component. The two histograms share
 *  the `HistogramBucket[]` shape for the same reason.
 *
 *  `total_outcomes` is the global denominator captioned at the top of the
 *  page ("Based on N evaluated signals") — answers the statistical-thinness
 *  question every track-record reader has. Cold-boot returns 0 and the page
 *  renders the empty state. */
export interface TrackRecordBreakdown {
  total_outcomes: number;
  by_detector: BreakdownStat[];
  by_asset: BreakdownStat[];
  by_confidence_bucket: BreakdownStat[];
  by_direction: BreakdownStat[];
  mfe_histogram: HistogramBucket[];
  mae_histogram: HistogramBucket[];
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
