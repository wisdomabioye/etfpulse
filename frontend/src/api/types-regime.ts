/**
 * TypeScript mirrors of the backend Pydantic DTOs — regime shapes.
 *
 * Source of truth: etfpulse/backend/etfpulse/api/schemas/regime.py
 * Update both when the API contract changes.
 */

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

/** One day of the `/regime` history strip. `regime` is null only on legacy
 *  pre-Stage-7 snapshot rows. */
export interface RegimeHistoryItem {
  date: string;
  regime: MarketRegime | null;
}

export interface RegimeHistoryResponse {
  history: RegimeHistoryItem[];
}
