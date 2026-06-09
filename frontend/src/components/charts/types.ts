import type { SignalType, TimeHorizon } from '../../api/types';

/** One confidence-bucket cell on the calibration curve. `insufficient` cells
 *  (n below the min-samples gate) render as "—" with no point/whisker. */
export interface CalibrationCell {
  hit: number;
  ci_low: number;
  ci_high: number;
  n: number;
  wins: number;
  insufficient: boolean;
}

/** One row of the hit-rate-by-horizon bars. `n` is optional: some sources
 *  (the track-record summary's per-horizon rates) carry the rate without a
 *  per-bucket sample count, in which case the bar omits the "n=" caption. */
export interface HitRateRow {
  horizon: TimeHorizon;
  hit: number;
  n?: number;
}

/** One detector's leaderboard row (empirical hit rate + Wilson CI). */
export interface LeaderboardRow {
  key: SignalType;
  hit: number;
  ci_low: number;
  ci_high: number;
  n: number;
}
