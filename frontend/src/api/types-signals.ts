/**
 * TypeScript mirrors of the backend Pydantic DTOs — signal shapes.
 *
 * Source of truth: etfpulse/backend/etfpulse/api/schemas/signals.py
 * Update both when the API contract changes.
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

  /** PR I.2 — confirmation score 0..3, summing per-factor votes (price,
   *  regime, news). NULL on wait / AI-failed / pre-I.2 historical rows.
   *  List shape carries only the integer; the breakdown lives on detail. */
  confirmation_score: number | null;
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
  /** PR I.3b — nullable for MARKET (regime_shift) outcomes, which carry
   *  the composite story in `composite_return_pct` instead. Single-asset
   *  outcomes always populate this. */
  price_at_signal: number | null;
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
  /** PR I.3b — composite BTC+ETH weighted return for MARKET (regime_shift)
   *  outcomes. Signed fraction (0.024 = +2.4%). NULL on single-asset rows.
   *  Mutually exclusive with `price_at_signal` / `entry_price` etc. — when
   *  this is set, those baseline fields are NULL by design. */
  composite_return_pct: number | null;
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

  /** PR I.2 — confirmation score 0..3 and per-factor breakdown.
   *  `confirmation_score` is the sum of votes across factors; NULL on
   *  wait/AI-failed/MARKET/pre-I.2 rows. `factor_votes` is the raw JSONB
   *  the backend wrote — keys are factor names (`price`, `regime`, `news`),
   *  each value carries at minimum `vote: -1 | 0 | +1` plus factor-specific
   *  diagnostic fields (e.g. price's `pct_change`, regime's `regime`). Both
   *  null together on rows where scoring didn't apply. */
  confirmation_score: number | null;
  factor_votes: Record<string, FactorVote> | null;
}

/** PR I.2 — per-factor vote shape on `SignalDetail.factor_votes`. Backend
 *  writes JSONB so the read-side type is intentionally permissive: a
 *  guaranteed `vote` field plus factor-specific diagnostic fields the UI
 *  can probe via `unknown` lookups. */
export interface FactorVote {
  vote: -1 | 0 | 1;
  [key: string]: unknown;
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
