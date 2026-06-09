/**
 * Tests for the R0 numeric formatters added to `format.ts`.
 *
 * Each formatter has a null/undefined "—" path (negative) and magnitude /
 * sign / decimal paths (positive). These mirror the prototype's `fmt.*`
 * exactly — drift here is design drift.
 */

import { describe, expect, it } from 'vitest';

import {
  formatAbsUtc,
  formatCompactFlow,
  formatConfidence,
  formatNumber,
  formatPct,
  formatPctRaw,
  formatPrice,
  formatRemaining,
  formatSignedPct,
  hoursUntil,
  isWithin,
  trimDecimal,
} from './format';

describe('trimDecimal', () => {
  it('strips trailing zeros from NUMERIC-string values', () => {
    expect(trimDecimal('0.002000000000000000')).toBe('0.002');
    expect(trimDecimal('65000.00000000')).toBe('65000');
    expect(trimDecimal('0.010000000000000000')).toBe('0.01');
    expect(trimDecimal('1.50')).toBe('1.5');
  });
  it('leaves integers + non-trailing-zero decimals intact', () => {
    expect(trimDecimal('3')).toBe('3');
    expect(trimDecimal('100')).toBe('100');
    expect(trimDecimal('0.125')).toBe('0.125');
    expect(trimDecimal(42)).toBe('42');
  });
  it('null / undefined / empty → em dash', () => {
    expect(trimDecimal(null)).toBe('—');
    expect(trimDecimal(undefined)).toBe('—');
    expect(trimDecimal('')).toBe('—');
  });
});

describe('formatPrice', () => {
  it('uses 0 decimals at ≥ 1000', () => {
    expect(formatPrice(84200.5)).toBe('$84,201');
    expect(formatPrice(1000)).toBe('$1,000');
  });
  it('uses 2 decimals in [1, 1000)', () => {
    expect(formatPrice(42.5)).toBe('$42.50');
    expect(formatPrice(1)).toBe('$1.00');
  });
  it('uses 4 decimals below 1', () => {
    expect(formatPrice(0.1234)).toBe('$0.1234');
    expect(formatPrice(0)).toBe('$0.0000');
  });
  it('honors an explicit dp override', () => {
    expect(formatPrice(84200.5, 2)).toBe('$84,200.50');
    expect(formatPrice(0.5, 0)).toBe('$1'); // rounds at 0dp
  });
  it('returns em-dash for null/undefined', () => {
    expect(formatPrice(null)).toBe('—');
    expect(formatPrice(undefined)).toBe('—');
  });
});

describe('formatCompactFlow', () => {
  it('emits billions with sign at ≥ 1000 (millions input)', () => {
    expect(formatCompactFlow(1500)).toBe('+$1.5B');
    expect(formatCompactFlow(-2000)).toBe('-$2.0B');
  });
  it('emits millions with sign below 1000', () => {
    expect(formatCompactFlow(300)).toBe('+$300M');
    expect(formatCompactFlow(-45)).toBe('-$45M');
  });
  it('treats zero as a positive sign', () => {
    expect(formatCompactFlow(0)).toBe('+$0M');
  });
  it('returns em-dash for null/undefined', () => {
    expect(formatCompactFlow(null)).toBe('—');
    expect(formatCompactFlow(undefined)).toBe('—');
  });
});

describe('formatPct', () => {
  it('scales a fraction to a percent (default 1dp)', () => {
    expect(formatPct(0.42)).toBe('42.0%');
    expect(formatPct(1)).toBe('100.0%');
  });
  it('honors a dp override', () => {
    expect(formatPct(0.4237, 2)).toBe('42.37%');
    expect(formatPct(0.5, 0)).toBe('50%');
  });
  it('returns em-dash for null/undefined', () => {
    expect(formatPct(null)).toBe('—');
    expect(formatPct(undefined)).toBe('—');
  });
});

describe('formatPctRaw', () => {
  it('formats an already-percent value without ×100', () => {
    expect(formatPctRaw(42)).toBe('42.0%');
    expect(formatPctRaw(7.25, 2)).toBe('7.25%');
  });
  it('returns em-dash for null/undefined', () => {
    expect(formatPctRaw(null)).toBe('—');
  });
});

describe('formatSignedPct', () => {
  it('forces a leading + on non-negatives', () => {
    expect(formatSignedPct(2.3)).toBe('+2.30%');
    expect(formatSignedPct(0)).toBe('+0.00%');
  });
  it('keeps the native minus on negatives', () => {
    expect(formatSignedPct(-1)).toBe('-1.00%');
  });
  it('honors dp + custom suffix (incl empty)', () => {
    expect(formatSignedPct(3.14159, 3)).toBe('+3.142%');
    expect(formatSignedPct(5, 0, '')).toBe('+5');
  });
  it('returns em-dash for null/undefined', () => {
    expect(formatSignedPct(null)).toBe('—');
  });
});

describe('formatConfidence', () => {
  it('renders X/10', () => {
    expect(formatConfidence(8)).toBe('8/10');
    expect(formatConfidence(0)).toBe('0/10');
  });
  it('rounds an averaged float to 1 decimal (no long tail, no trailing .0)', () => {
    expect(formatConfidence(3.6153846153846154)).toBe('3.6/10');
    expect(formatConfidence(7.04)).toBe('7/10');
    expect(formatConfidence(5.92)).toBe('5.9/10');
  });
  it('returns em-dash for null/undefined', () => {
    expect(formatConfidence(null)).toBe('—');
    expect(formatConfidence(undefined)).toBe('—');
  });
});

describe('formatNumber', () => {
  it('groups thousands', () => {
    expect(formatNumber(1234567)).toBe('1,234,567');
    expect(formatNumber(0)).toBe('0');
  });
  it('returns em-dash for null/undefined', () => {
    expect(formatNumber(null)).toBe('—');
  });
});

describe('formatAbsUtc', () => {
  it('renders an absolute UTC string with the UTC suffix', () => {
    const out = formatAbsUtc('2026-06-08T04:30:00Z');
    expect(out).toContain('08 Jun 2026 04:30:00');
    expect(out.endsWith('UTC')).toBe(true);
    expect(out).not.toContain('GMT');
  });
  it('returns the raw input on an unparseable string', () => {
    expect(formatAbsUtc('not-a-date')).toBe('not-a-date');
  });
});

describe('isWithin', () => {
  it('is true for a timestamp inside the window', () => {
    expect(isWithin(new Date().toISOString(), 60_000)).toBe(true);
  });
  it('is false for a timestamp outside the window', () => {
    const old = new Date(Date.now() - 10 * 60_000).toISOString();
    expect(isWithin(old, 60_000)).toBe(false);
  });
  it('is false for an unparseable timestamp', () => {
    expect(isWithin('not-a-date', 60_000)).toBe(false);
  });
});

describe('hoursUntil', () => {
  it('is positive for a future timestamp', () => {
    const future = new Date(Date.now() + 70 * 3_600_000).toISOString();
    expect(hoursUntil(future)).toBeGreaterThan(69);
  });
  it('is negative for a past timestamp', () => {
    const past = new Date(Date.now() - 5 * 3_600_000).toISOString();
    expect(hoursUntil(past)).toBeLessThan(0);
  });
  it('is null for a bad date', () => {
    expect(hoursUntil('nope')).toBeNull();
  });
});

describe('formatRemaining', () => {
  it('shows hours under two days', () => {
    expect(formatRemaining(40)).toBe('~40h remaining');
    expect(formatRemaining(5.4)).toBe('~5h remaining');
  });
  it('shows d + h beyond two days (precise, not coarse)', () => {
    expect(formatRemaining(50)).toBe('~2d 2h remaining'); // 50h = 2d 2h
    expect(formatRemaining(70)).toBe('~2d 22h remaining'); // 70h = 2d 22h
    expect(formatRemaining(168)).toBe('~7d 0h remaining');
  });
  it('reports due when past', () => {
    expect(formatRemaining(0)).toBe('evaluation due');
    expect(formatRemaining(-3)).toBe('evaluation due');
  });
});
