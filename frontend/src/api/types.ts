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

export type AssetSymbol = 'BTC' | 'ETH';

export type SignalType =
  | 'flow_anomaly'
  | 'magnitude'
  | 'acceleration'
  | 'divergence'
  | 'regime_shift';

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
}

export interface SignalOutcome {
  entry_price: number | null;
  stop_price: number | null;
  target_price: number | null;
  price_at_signal: number;
  price_after_24h: number | null;
  price_after_72h: number | null;
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
