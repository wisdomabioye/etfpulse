import { describe, expect, it } from 'vitest';

import { checkOrderAgainstCaps, orderCost, orderNotional, unrealizedPnl } from './positionMath';

describe('orderNotional', () => {
  it('is |size| × price', () => {
    expect(orderNotional(0.01, 65000)).toBe(650);
    expect(orderNotional(2, 3000)).toBe(6000);
  });
});

describe('unrealizedPnl', () => {
  it('long profits when mark > entry (buy === long)', () => {
    const a = unrealizedPnl({ side: 'long', size: 0.01, entry: 60000, mark: 65000 });
    const b = unrealizedPnl({ side: 'buy', size: 0.01, entry: 60000, mark: 65000 });
    expect(a).toEqual(b);
    expect(a!.pnl).toBeCloseTo(50); // (65000-60000)*0.01
    expect(a!.pnlPct).toBeCloseTo(8.3333, 3); // 5000/60000*100
  });

  it('long loses when mark < entry', () => {
    const r = unrealizedPnl({ side: 'long', size: 0.01, entry: 65000, mark: 60000 });
    expect(r!.pnl).toBeCloseTo(-50);
    expect(r!.pnlPct).toBeLessThan(0);
  });

  it('short inverts the sign (sell === short)', () => {
    const r = unrealizedPnl({ side: 'short', size: 0.01, entry: 65000, mark: 60000 });
    expect(r!.pnl).toBeCloseTo(50); // short profits as price falls
    expect(r!.pnlPct).toBeGreaterThan(0);
    expect(unrealizedPnl({ side: 'sell', size: 0.01, entry: 65000, mark: 60000 })).toEqual(r);
  });

  it('returns null for invalid inputs (zero/neg entry or size, NaN mark)', () => {
    expect(unrealizedPnl({ side: 'long', size: 0, entry: 60000, mark: 65000 })).toBeNull();
    expect(unrealizedPnl({ side: 'long', size: 0.01, entry: 0, mark: 65000 })).toBeNull();
    expect(unrealizedPnl({ side: 'long', size: 0.01, entry: 60000, mark: NaN })).toBeNull();
  });
});

describe('orderCost', () => {
  it('computes notional + fee + total', () => {
    const c = orderCost({ size: 0.01, price: 65000, feeRate: 0.0005 });
    expect(c!.notional).toBe(650);
    expect(c!.fee).toBeCloseTo(0.325); // 650 * 0.0005
    expect(c!.total).toBeCloseTo(650.325);
  });

  it('treats a missing/zero/negative fee rate as zero fee', () => {
    expect(orderCost({ size: 0.01, price: 65000, feeRate: 0 })!.fee).toBe(0);
    expect(orderCost({ size: 0.01, price: 65000, feeRate: NaN })!.fee).toBe(0);
    expect(orderCost({ size: 0.01, price: 65000, feeRate: -0.1 })!.fee).toBe(0);
  });

  it('returns null when size or price is invalid', () => {
    expect(orderCost({ size: 0, price: 65000, feeRate: 0.0005 })).toBeNull();
    expect(orderCost({ size: 0.01, price: 0, feeRate: 0.0005 })).toBeNull();
    expect(orderCost({ size: NaN, price: 65000, feeRate: 0.0005 })).toBeNull();
  });
});

describe('checkOrderAgainstCaps', () => {
  const base = {
    reduceOnly: false,
    dailyCap: 10000,
    dailyUsed: 4000,
    perSymbolCap: 5000,
    perSymbolUsed: 3000 as number | null,
  };

  it('computes remaining headroom (floored at 0)', () => {
    const r = checkOrderAgainstCaps({ ...base, notional: 100 });
    expect(r.dailyRemaining).toBe(6000);
    expect(r.perSymbolRemaining).toBe(2000);
  });

  it('floors remaining at 0 when already over', () => {
    const r = checkOrderAgainstCaps({
      ...base,
      notional: 100,
      dailyUsed: 12000,
      perSymbolUsed: 6000,
    });
    expect(r.dailyRemaining).toBe(0);
    expect(r.perSymbolRemaining).toBe(0);
  });

  it('flags per-symbol breach before daily breach (the 403 the user hit)', () => {
    // used 3000 + 2500 = 5500 > 5000 per-symbol, but 4000 + 2500 = 6500 < 10000 daily.
    const r = checkOrderAgainstCaps({ ...base, notional: 2500 });
    expect(r.exceedsPerSymbol).toBe(true);
    expect(r.exceedsDaily).toBe(false);
  });

  it('flags daily breach', () => {
    const r = checkOrderAgainstCaps({ ...base, notional: 7000, perSymbolUsed: 0 });
    expect(r.exceedsDaily).toBe(true);
  });

  it('reduce-only is cap-exempt (mirrors backend CAP-EXEMPT)', () => {
    const r = checkOrderAgainstCaps({ ...base, notional: 999999, reduceOnly: true });
    expect(r.exceedsDaily).toBe(false);
    expect(r.exceedsPerSymbol).toBe(false);
    // headroom is still reported for display.
    expect(r.dailyRemaining).toBe(6000);
  });

  it('per-symbol unknown (asset not scoped) reports null + never breaches per-symbol', () => {
    const r = checkOrderAgainstCaps({ ...base, notional: 999999, perSymbolUsed: null });
    expect(r.perSymbolRemaining).toBeNull();
    expect(r.exceedsPerSymbol).toBe(false);
    expect(r.exceedsDaily).toBe(true);
  });

  it('zero/invalid notional never breaches', () => {
    expect(checkOrderAgainstCaps({ ...base, notional: 0 }).exceedsDaily).toBe(false);
    expect(checkOrderAgainstCaps({ ...base, notional: NaN }).exceedsPerSymbol).toBe(false);
  });
});
