/**
 * TypeScript mirrors of the backend Pydantic DTOs — track-record + analytics.
 *
 * Source of truth: etfpulse/backend/etfpulse/api/schemas/{track_record,analytics}.py
 * Update both when the API contract changes.
 */

import type { AssetSymbol, SignalType, HorizonLabel } from './types-signals';

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
  /** PR I.3b — nullable for MARKET outcomes (see `SignalOutcome`). */
  price_at_signal: number | null;
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

  /** PR I.3b — see `SignalOutcome.composite_return_pct`. NULL on single-asset rows. */
  composite_return_pct: number | null;

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

/** PR I.1 — one cell of the reliability curve. Mirrors
 *  `CalibrationBucketOut` on the backend. `bucket_floor` and
 *  `bucket_ceiling` are inclusive [1..10] confidence range; with the
 *  default `bucket_size=2`, ranges are (1,2), (3,4), (5,6), (7,8), (9,10).
 *  `hit_rate`, `ci_low`, `ci_high` are 0..1 fractions (not percents) and
 *  null when the cell is empty OR below `min_samples` — FE renders "—"
 *  in both cases. */
export interface CalibrationBucket {
  bucket_floor: number;
  bucket_ceiling: number;
  horizon: HorizonLabel;
  n_samples: number;
  wins: number;
  losses: number;
  hit_rate: number | null;
  ci_low: number | null;
  ci_high: number | null;
}

/** PR I.1 — full reliability surface for one (prompt_version, lookback)
 *  cohort. `buckets` is ALWAYS the full grid (every bucket × horizon
 *  combination); empty cells carry `n_samples=0` + `hit_rate=null` so
 *  the FE can render fixed-position tiles without layout shift. */
export interface CalibrationResponse {
  ai_prompt_version: string;
  lookback_days: number;
  min_samples: number;
  bucket_size: number;
  buckets: CalibrationBucket[];
}

/** PR I.3 — one cell of the per-detector precision grid (a detector ×
 *  horizon intersection, OR the across-horizons total cell). Mirrors
 *  `DetectorHorizonCellOut` on the backend.
 *
 *  `hit_rate`, `ci_low`, `ci_high` are 0..1 fractions and null when the
 *  cell is empty OR below `min_samples` — FE renders "—" in both cases.
 *  `wins`/`losses`/`n_samples` are populated regardless so the card can
 *  show "n=8 (need 3 more)" hover text on insufficient cells. */
export interface DetectorHorizonCell {
  n_samples: number;
  wins: number;
  losses: number;
  hit_rate: number | null;
  ci_low: number | null;
  ci_high: number | null;
}

/** PR I.3 — one detector's slice: per-horizon cells + the across-horizons
 *  total. Mirrors `DetectorRowOut` on the backend.
 *
 *  `signal_type` is intentionally a free string (not the `SignalType`
 *  union): the backend includes legacy/removed detectors with historical
 *  data, and a new detector lands without an API contract change. The FE
 *  can narrow to `SignalType` for label/colour lookups via a runtime
 *  check (`signal_type in KNOWN_SIGNAL_TYPES`) when needed.
 *
 *  Option C rendering (the v1 layout): the card displays `total` per
 *  detector and uses `horizons` for hover/drill-down. Backend emits both
 *  in every response so a future "show horizon breakdown" toggle is a
 *  pure FE change. */
export interface DetectorRow {
  signal_type: string;
  horizons: Record<HorizonLabel, DetectorHorizonCell>;
  total: DetectorHorizonCell;
}

/** PR I.3 — full per-detector precision report for one (prompt_version,
 *  lookback) cohort.
 *
 *  `detectors` is ordered: registered detectors in `ALL_DETECTORS`
 *  precedence first (regime_shift excluded by design — pending PR I.3b
 *  MARKET-signal composite scoring), then any legacy signal_types found
 *  in historical data, sorted alphabetically. Even on cold start (zero
 *  evaluated outcomes) the registered detectors are present with all-zero
 *  cells, so the FE renders stable rows. */
export interface PerDetectorResponse {
  ai_prompt_version: string;
  lookback_days: number;
  min_samples: number;
  detectors: DetectorRow[];
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
