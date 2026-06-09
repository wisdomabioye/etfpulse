import { describe, expect, it } from 'vitest';

import { sideDisplay } from './orderSide';

describe('sideDisplay', () => {
  it('perps positions are directional Long / Short with arrows', () => {
    expect(sideDisplay('sodex_perps', 'long')).toMatchObject({
      label: 'Long',
      glyph: '▲',
      token: '--win',
      positive: true,
    });
    expect(sideDisplay('sodex_perps', 'short')).toMatchObject({
      label: 'Short',
      glyph: '▼',
      token: '--loss',
      positive: false,
    });
  });

  it('perps orders (buy/sell) map to Long / Short', () => {
    expect(sideDisplay('sodex_perps', 'buy').label).toBe('Long');
    expect(sideDisplay('sodex_perps', 'sell').label).toBe('Short');
  });

  it('spot uses Buy / Sell with NO directional arrow (no shorting on spot)', () => {
    expect(sideDisplay('sodex_spot', 'buy')).toMatchObject({
      label: 'Buy',
      glyph: '',
      token: '--win',
      positive: true,
    });
    expect(sideDisplay('sodex_spot', 'sell')).toMatchObject({
      label: 'Sell',
      glyph: '',
      token: '--loss',
      positive: false,
    });
  });

  it('spot positions (stored long-only) render as Buy', () => {
    expect(sideDisplay('sodex_spot', 'long').label).toBe('Buy');
    expect(sideDisplay('sodex_spot', 'long').glyph).toBe('');
  });
});
