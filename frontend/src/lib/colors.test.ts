/**
 * Tests for the data-driven color resolvers (R0).
 *
 * Every union member is asserted (full coverage of the Record maps) plus the
 * tier boundaries for confidence + the CI-tone logic. These mirror the
 * prototype's `assetColor / confColor / confSoft / ciTone / actionMeta`.
 */

import { describe, expect, it } from 'vitest';

import { ASSETS, SIGNAL_TYPES } from './constants';
import type { MarketRegime, SuggestedAction } from '../api/types';

import {
  actionMeta,
  assetColorToken,
  ciToneToken,
  confColorToken,
  confSoftToken,
  detectorColorToken,
  regimeColorToken,
} from './colors';

const REGIMES: MarketRegime[] = [
  'accumulation',
  'markup',
  'distribution',
  'markdown',
  'uncertain',
];

describe('detectorColorToken', () => {
  it('maps each detector to its identity token', () => {
    expect(detectorColorToken('flow_anomaly')).toBe('--det-flow');
    expect(detectorColorToken('magnitude')).toBe('--det-mag');
    expect(detectorColorToken('acceleration')).toBe('--det-accel');
    expect(detectorColorToken('divergence')).toBe('--det-div');
    expect(detectorColorToken('regime_shift')).toBe('--det-regime');
  });
  it('returns a token for every SignalType', () => {
    for (const t of SIGNAL_TYPES) {
      expect(detectorColorToken(t)).toMatch(/^--det-/);
    }
  });
});

describe('regimeColorToken', () => {
  it('maps each regime to its state token', () => {
    expect(regimeColorToken('accumulation')).toBe('--reg-accum');
    expect(regimeColorToken('markup')).toBe('--reg-markup');
    expect(regimeColorToken('distribution')).toBe('--reg-dist');
    expect(regimeColorToken('markdown')).toBe('--reg-markdown');
    expect(regimeColorToken('uncertain')).toBe('--reg-uncertain');
  });
  it('returns a token for every regime', () => {
    for (const r of REGIMES) {
      expect(regimeColorToken(r)).toMatch(/^--reg-/);
    }
  });
});

describe('assetColorToken', () => {
  it('maps each asset to its brand token', () => {
    expect(assetColorToken('BTC')).toBe('--btc');
    expect(assetColorToken('ETH')).toBe('--eth');
    expect(assetColorToken('MARKET')).toBe('--market');
  });
  it('returns a token for every asset', () => {
    for (const a of ASSETS) {
      expect(assetColorToken(a)).toMatch(/^--/);
    }
  });
});

describe('confColorToken', () => {
  it('ramps across the four tiers at the right boundaries', () => {
    expect(confColorToken(10)).toBe('--win');
    expect(confColorToken(8)).toBe('--win'); // ≥8
    expect(confColorToken(7)).toBe('--conf-mid'); // ≥6
    expect(confColorToken(6)).toBe('--conf-mid');
    expect(confColorToken(5)).toBe('--warn'); // ≥4
    expect(confColorToken(4)).toBe('--warn');
    expect(confColorToken(3)).toBe('--loss'); // <4
    expect(confColorToken(1)).toBe('--loss');
  });
});

describe('confSoftToken', () => {
  it('ramps across the three soft tiers at the right boundaries', () => {
    expect(confSoftToken(8)).toBe('--win-soft'); // ≥8
    expect(confSoftToken(7)).toBe('--warn-soft'); // ≥4
    expect(confSoftToken(4)).toBe('--warn-soft');
    expect(confSoftToken(3)).toBe('--loss-soft'); // <4
  });
});

describe('ciToneToken', () => {
  it('is win when the lower bound clears 0.5', () => {
    expect(ciToneToken(0.51, 0.9)).toBe('--win');
  });
  it('is loss when the upper bound is below 0.5', () => {
    expect(ciToneToken(0.1, 0.49)).toBe('--loss');
  });
  it('is warn (neutral) when the interval straddles 0.5', () => {
    expect(ciToneToken(0.4, 0.6)).toBe('--warn');
  });
  it('applies the bound rules independently when one side is null', () => {
    // A null lower bound can't win, but a high < 0.5 still loses.
    expect(ciToneToken(null, 0.4)).toBe('--loss');
    // A low > 0.5 wins regardless of a null upper bound.
    expect(ciToneToken(0.6, null)).toBe('--win');
    // Neither rule fires → neutral.
    expect(ciToneToken(null, 0.6)).toBe('--warn');
    expect(ciToneToken(0.3, null)).toBe('--warn');
    expect(ciToneToken(null, null)).toBe('--warn');
    expect(ciToneToken(undefined, undefined)).toBe('--warn');
  });
});

describe('actionMeta', () => {
  it('maps consider long → win/Long/▲', () => {
    expect(actionMeta('consider long')).toEqual({
      tone: '--win',
      label: 'Long',
      full: 'Consider long',
      arrow: '▲',
    });
  });
  it('maps consider short → loss/Short/▼', () => {
    expect(actionMeta('consider short')).toEqual({
      tone: '--loss',
      label: 'Short',
      full: 'Consider short',
      arrow: '▼',
    });
  });
  it('maps wait → warn/Wait/■', () => {
    expect(actionMeta('wait')).toEqual({
      tone: '--warn',
      label: 'Wait',
      full: 'Wait',
      arrow: '■',
    });
  });
  it('covers every SuggestedAction', () => {
    const actions: SuggestedAction[] = ['consider long', 'consider short', 'wait'];
    for (const a of actions) {
      expect(actionMeta(a).tone).toMatch(/^--/);
    }
  });
});
