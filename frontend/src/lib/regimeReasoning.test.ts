import { describe, expect, it } from 'vitest';

import { asNumber, readContributions, readDominance } from './regimeReasoning';

describe('asNumber', () => {
  it('accepts numbers and numeric strings (the classifier writes str(Decimal))', () => {
    expect(asNumber(54.2)).toBe(54.2);
    expect(asNumber('0.5943')).toBe(0.5943);
    expect(asNumber('-2.3')).toBe(-2.3);
  });
  it('rejects non-numeric / non-number values', () => {
    expect(asNumber('n/a')).toBeNull();
    expect(asNumber(null)).toBeNull();
    expect(asNumber(undefined)).toBeNull();
    expect(asNumber({})).toBeNull();
    expect(asNumber(NaN)).toBeNull();
  });
});

describe('readDominance', () => {
  it('parses the string-valued dominance block', () => {
    const d = readDominance({
      dominance: { available: true, btc_dominance: '0.5943', btc_change_pct_24h: '-1.2', sector_count: 16 },
    });
    expect(d.btcDominance).toBe(0.5943);
    expect(d.change24h).toBe(-1.2);
    expect(d.sectorCount).toBe(16);
  });
  it('returns nulls when the block is absent or failed', () => {
    expect(readDominance({})).toEqual({ btcDominance: null, change24h: null, sectorCount: null });
    expect(readDominance({ dominance: { available: false, fetch_error: 'RateLimit' } })).toEqual({
      btcDominance: null,
      change24h: null,
      sectorCount: null,
    });
  });
});

describe('readContributions', () => {
  it('extracts flow + news directional scores', () => {
    const c = readContributions({ flow: { score: 3 }, news: { score: -1 } });
    expect(c).toEqual([
      { label: 'Flow', score: 3 },
      { label: 'News', score: -1 },
    ]);
  });
  it('skips components without a numeric score', () => {
    expect(readContributions({ flow: { score: 2 } })).toEqual([{ label: 'Flow', score: 2 }]);
    expect(readContributions({})).toEqual([]);
  });
});
