import type { CalibrationResponse, HorizonLabel } from '../api/types';
import type { CalibrationCell } from '../components/charts';

/**
 * Map a `CalibrationResponse` to the `CalibrationCell[]` the `CalibrationCurve`
 * chart consumes, for one horizon. Buckets are filtered to the horizon and
 * sorted by `bucket_floor` so the x-axis reads low→high confidence. A cell is
 * `insufficient` when the backend reported a null hit-rate OR `n_samples` is
 * below the cohort's `min_samples` gate — the chart renders those as "—".
 *
 * Shared by the Home calibration teaser (R4) and the Proof page (R5) so the
 * mapping lives in exactly one place.
 */
export function calibrationCells(
  resp: CalibrationResponse,
  horizon: HorizonLabel,
): CalibrationCell[] {
  return resp.buckets
    .filter((b) => b.horizon === horizon)
    .sort((a, b) => a.bucket_floor - b.bucket_floor)
    .map((b) => ({
      hit: b.hit_rate ?? 0,
      ci_low: b.ci_low ?? 0,
      ci_high: b.ci_high ?? 0,
      n: b.n_samples,
      wins: b.wins,
      insufficient: b.hit_rate === null || b.n_samples < resp.min_samples,
    }));
}
