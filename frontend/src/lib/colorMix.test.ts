/**
 * Tests for the `colorMix` data-driven color helpers (R0).
 *
 * These produce the exact CSS strings the prototype uses for SVG fills and
 * tinted backgrounds — the percentages are the design spec, so the strings
 * are asserted verbatim.
 */

import { describe, expect, it } from 'vitest';

import { colorMix, cssVar } from './colorMix';

describe('cssVar', () => {
  it('wraps a token in var()', () => {
    expect(cssVar('--acc')).toBe('var(--acc)');
    expect(cssVar('--det-flow')).toBe('var(--det-flow)');
  });
});

describe('colorMix', () => {
  it('mixes a token toward transparent by default', () => {
    expect(colorMix('--det-flow', 9)).toBe('color-mix(in oklab, var(--det-flow) 9%, transparent)');
  });
  it('preserves the exact design percentage', () => {
    expect(colorMix('--win', 14)).toBe('color-mix(in oklab, var(--win) 14%, transparent)');
    expect(colorMix('--acc', 38)).toBe('color-mix(in oklab, var(--acc) 38%, transparent)');
  });
  it('mixes toward an explicit base (e.g. a surface token)', () => {
    expect(colorMix('--warn', 12, cssVar('--bg-2'))).toBe(
      'color-mix(in oklab, var(--warn) 12%, var(--bg-2))',
    );
  });
});
