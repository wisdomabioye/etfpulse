/**
 * Tests for the R0 display-config constants.
 *
 * The key assertion is completeness: every domain-enum member must have a
 * display entry (a missing key would render blank in the UI). `Record<Union,
 * …>` already enforces this at compile time, but the runtime checks guard
 * against a future `as` cast sneaking a partial map past the type-checker.
 */

import { describe, expect, it } from 'vitest';

import { ASSETS, SIGNAL_TYPES } from './constants';
import {
  BUCKET_IDEAL,
  CONF_BUCKETS,
  DETECTORS,
  HORIZONS,
  ORDER_STATUS,
  ORDER_STATUSES,
  REGIMES,
} from './constants';
import type { MarketRegime } from '../api/types';

const REGIME_KEYS: MarketRegime[] = [
  'accumulation',
  'markup',
  'distribution',
  'markdown',
  'uncertain',
];

describe('DETECTORS', () => {
  it('has an entry for every SignalType with non-empty fields', () => {
    for (const t of SIGNAL_TYPES) {
      const d = DETECTORS[t];
      expect(d).toBeDefined();
      expect(d.label.length).toBeGreaterThan(0);
      expect(d.short.length).toBeGreaterThan(0);
      expect(d.catches.length).toBeGreaterThan(0);
    }
  });
  it('does not store a color (color lives in colors.ts)', () => {
    expect(DETECTORS.flow_anomaly).not.toHaveProperty('color');
  });
});

describe('REGIMES', () => {
  it('has a label + glyph for every regime', () => {
    for (const r of REGIME_KEYS) {
      expect(REGIMES[r].label.length).toBeGreaterThan(0);
      expect(REGIMES[r].glyph.length).toBeGreaterThan(0);
    }
  });
});

describe('ORDER_STATUS', () => {
  it('has a label + token tone for every status', () => {
    for (const s of ORDER_STATUSES) {
      expect(ORDER_STATUS[s].label.length).toBeGreaterThan(0);
      expect(ORDER_STATUS[s].tone).toMatch(/^--/);
    }
  });
  it('covers exactly the eight known statuses', () => {
    expect(ORDER_STATUSES).toHaveLength(8);
    expect(Object.keys(ORDER_STATUS).sort()).toEqual([...ORDER_STATUSES].sort());
  });
});

describe('HORIZONS', () => {
  it('lists the three horizons with window labels', () => {
    expect(HORIZONS.map((h) => h.key)).toEqual(['scalp', 'swing', 'position']);
    for (const h of HORIZONS) {
      expect(h.label.length).toBeGreaterThan(0);
      expect(h.window.length).toBeGreaterThan(0);
    }
  });
});

describe('calibration x-axis config', () => {
  it('aligns bucket labels with ideal midpoints', () => {
    expect(CONF_BUCKETS).toHaveLength(5);
    expect(BUCKET_IDEAL).toHaveLength(CONF_BUCKETS.length);
    // Midpoints strictly increasing across buckets.
    for (let i = 1; i < BUCKET_IDEAL.length; i++) {
      expect(BUCKET_IDEAL[i]).toBeGreaterThan(BUCKET_IDEAL[i - 1]);
    }
  });
});

describe('domain enums (sanity)', () => {
  it('exposes the expected assets + signal types', () => {
    expect(ASSETS).toEqual(['BTC', 'ETH', 'MARKET']);
    expect(SIGNAL_TYPES).toHaveLength(5);
  });
});
