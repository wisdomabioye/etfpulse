/**
 * Backtest admin route hooks + types (PR P2.4 / task #204).
 *
 * Mirrors `etfpulse/api/schemas/backtest.py` field-for-field. When the
 * backend schema changes, this file changes in the same PR — the type
 * shapes are the only contract between FE form/results panel and the
 * orchestrator output.
 *
 * Why a feature-scoped module instead of appending to `api/types.ts`
 * and `api/queries.ts`: both files are already past the 200–300 LOC
 * cap (599 and 653 respectively). Future feature work should follow
 * the same per-feature seam.
 */

import { useMutation, useQuery } from '@tanstack/react-query';

import { apiGet, apiPost } from './client';

/* ─────────────────────────── types ─────────────────────────── */

/** Allowed scalar types for detector kwargs. Mirrors the Python
 *  `DetectorKwargValue = bool | int | float | str` union. TypeScript
 *  doesn't distinguish int/float — `number` covers both. */
export type DetectorKwargValue = boolean | number | string;

/** Per-bucket scoring rubric tag. Mirrors `ScoringVersion` in the
 *  backend schema. */
export type BacktestScoringVersion = 'v2' | 'market-v1';

/** Request body for `POST /api/admin/backtest`. */
export interface BacktestRequest {
  /** ISO date YYYY-MM-DD. */
  start: string;
  /** ISO date YYYY-MM-DD. */
  end: string;
  /** Per-detector kwarg overrides. Unset = use production defaults. */
  detector_overrides?: Record<string, Record<string, DetectorKwargValue>>;
  /** Opt-in to live AI on cache miss. Requires `X-Backtest-Allow-AI:
   *  yes` header in addition to the body flag. */
  allow_ai?: boolean;
}

/** One outcome row in the report. Mirrors
 *  `pipeline.backtest.BacktestOutcomeRow` field-for-field. */
export interface BacktestOutcomeRow {
  detector_name: string;
  signal_type: string;
  asset: string;
  signal_date: string;
  fingerprint: string;
  direction: string | null;
  confidence: number | null;
  hit_target: boolean | null;
  hit_stop: boolean | null;
  composite_return_pct: string | null;
  scoring_version: BacktestScoringVersion | null;
  window_hours: number | null;
  skip_reason: string | null;
}

/** One per-detector summary row. Mirrors
 *  `pipeline.backtest.BacktestPerDetector`. */
export interface BacktestPerDetector {
  detector_name: string;
  n_hits: number;
  n_scored: number;
  wins: number;
  losses: number;
  /** null when n_scored == 0 — keeps null vs 0.0 distinguishable so
   *  the FE renders "—" instead of "0.0%". */
  hit_rate: number | null;
}

/** Full report shape returned by `POST /api/admin/backtest`. */
export interface BacktestReport {
  start: string;
  end: string;
  ai_prompt_version: string;
  detector_configs: Record<string, Record<string, DetectorKwargValue>>;
  counters: Record<string, number>;
  per_detector: BacktestPerDetector[];
  outcomes: BacktestOutcomeRow[];
}

/** One constructor kwarg for a detector. The FE form uses `type_name`
 *  to pick an input widget; `default` is rendered as the initial
 *  value when present. */
export interface BacktestDetectorParam {
  name: string;
  /** "int" | "float" | "Decimal" | "bool" | "str" — FE picks input
   *  widget based on this. */
  type_name: string;
  has_default: boolean;
  default: DetectorKwargValue | null;
}

/** One detector entry from `GET /api/admin/backtest/detectors`. */
export interface BacktestDetector {
  name: string;
  signal_type: string;
  params: BacktestDetectorParam[];
}

/** Response shape of `GET /api/admin/backtest/detectors`. */
export interface BacktestDetectorsResponse {
  detectors: BacktestDetector[];
}

/* ─────────────────────────── hooks ─────────────────────────── */

/** Cache-key namespace for backtest queries — keeps invalidation
 *  scopes narrow without colliding with the existing admin/metrics
 *  scope. */
const KEY_BACKTEST_DETECTORS = ['backtest', 'detectors'] as const;

/**
 * List backtest-eligible detectors with their constructor kwarg
 * signatures. The registry only changes on deploy, so we cache
 * indefinitely (`staleTime: Infinity`) and hold in memory for the
 * tab's lifetime (`gcTime: Infinity`).
 *
 * The hook is disabled while `adminKey` is empty so we don't fire a
 * request with no auth — mirrors the convention `useAdminMetrics`
 * uses for the same reason.
 */
export function useBacktestDetectors(adminKey: string) {
  return useQuery({
    queryKey: [...KEY_BACKTEST_DETECTORS, adminKey],
    queryFn: () =>
      apiGet<BacktestDetectorsResponse>(
        '/api/admin/backtest/detectors',
        undefined,
        { 'X-Admin-Key': adminKey },
      ),
    enabled: adminKey.length > 0,
    staleTime: Infinity,
    gcTime: Infinity,
    retry: false,
  });
}

/**
 * Run a backtest sweep. Write-shaped UX (operator clicks "Run" →
 * waits → sees result) even though the server is read-only, so a
 * mutation hook matches the interaction better than a parameterised
 * query. Retry off — a 4xx/5xx is either bad input or a real
 * orchestrator failure; re-firing wastes a request worker for 10–60s.
 *
 * `allow_ai=true` requires both the body flag and the
 * `X-Backtest-Allow-AI: yes` header. The hook always sends the header
 * when `request.allow_ai` is true so the operator's body+confirm flow
 * doesn't need to surface the header step manually.
 */
export function useBacktest(adminKey: string) {
  return useMutation({
    mutationFn: (request: BacktestRequest) => {
      const headers: Record<string, string> = { 'X-Admin-Key': adminKey };
      if (request.allow_ai) {
        headers['X-Backtest-Allow-AI'] = 'yes';
      }
      return apiPost<BacktestReport>('/api/admin/backtest', request, headers);
    },
    retry: false,
  });
}
