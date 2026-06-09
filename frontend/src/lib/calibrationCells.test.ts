import { describe, expect, it } from 'vitest';

import type { CalibrationBucket, CalibrationResponse } from '../api/types';
import { calibrationCells } from './calibrationCells';

const bucket = (over: Partial<CalibrationBucket>): CalibrationBucket => ({
  bucket_floor: 1,
  bucket_ceiling: 2,
  horizon: 'swing',
  n_samples: 30,
  wins: 18,
  losses: 12,
  hit_rate: 0.6,
  ci_low: 0.45,
  ci_high: 0.72,
  ...over,
});

const resp = (buckets: CalibrationBucket[]): CalibrationResponse => ({
  ai_prompt_version: 'v3',
  lookback_days: 90,
  min_samples: 20,
  bucket_size: 2,
  buckets,
});

describe('calibrationCells', () => {
  it('filters to the horizon and sorts by bucket floor', () => {
    const r = resp([
      bucket({ bucket_floor: 5, hit_rate: 0.7 }),
      bucket({ bucket_floor: 1, hit_rate: 0.3 }),
      bucket({ bucket_floor: 9, horizon: 'position', hit_rate: 0.9 }),
    ]);
    const cells = calibrationCells(r, 'swing');
    expect(cells).toHaveLength(2); // position bucket excluded
    expect(cells[0].hit).toBe(0.3); // floor 1 first
    expect(cells[1].hit).toBe(0.7);
  });

  it('marks a cell insufficient when below min_samples or null hit_rate', () => {
    const r = resp([
      bucket({ bucket_floor: 1, n_samples: 5 }), // below min_samples 20
      bucket({ bucket_floor: 3, hit_rate: null, ci_low: null, ci_high: null, n_samples: 40 }),
      bucket({ bucket_floor: 5, n_samples: 40, hit_rate: 0.8 }),
    ]);
    const cells = calibrationCells(r, 'swing');
    expect(cells[0].insufficient).toBe(true); // low n
    expect(cells[1].insufficient).toBe(true); // null hit
    expect(cells[2].insufficient).toBe(false);
    // Nulls coalesce to 0 so the chart math never sees NaN.
    expect(cells[1].hit).toBe(0);
    expect(cells[1].ci_low).toBe(0);
  });
});
