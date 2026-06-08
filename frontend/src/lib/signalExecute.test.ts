/**
 * SIG2X helper tests — pure-function gate + URL/side/venue mappings.
 *
 * The same gate is enforced on the backend Telegram-keyboard side
 * (`pipeline/delivery.py:build_signal_keyboard`); the test cases here
 * encode the JS-side contract. If the Python side ever drifts (e.g.
 * accepts MARKET signals), backend-side tests are the matching
 * regression net — neither alone is sufficient.
 */

import { describe, expect, it } from 'vitest';

import type { Side, Venue } from '../api/execution';
import type { AIAnalysis, AssetSymbol, SuggestedAction } from '../api/types';

import {
  defaultVenueForSuggestedAction,
  isExecutableSignal,
  sideForSuggestedAction,
  signalExecuteHref,
} from './signalExecute';

interface MinimalSignal {
  asset: AssetSymbol;
  ai_analysis: Pick<AIAnalysis, 'suggested_action'> | null;
}

function mk(over: Partial<MinimalSignal> = {}): MinimalSignal {
  return {
    asset: 'BTC',
    ai_analysis: { suggested_action: 'consider long' },
    ...over,
  };
}

describe('isExecutableSignal', () => {
  it.each<[string, SuggestedAction]>([
    ['consider long', 'consider long'],
    ['consider short', 'consider short'],
  ])('returns true for BTC + %s', (_, action) => {
    expect(isExecutableSignal(mk({ ai_analysis: { suggested_action: action } }))).toBe(true);
  });

  it('returns true for ETH + actionable direction', () => {
    expect(isExecutableSignal(mk({ asset: 'ETH' }))).toBe(true);
  });

  it('returns false for MARKET (not a tradeable asset)', () => {
    expect(isExecutableSignal(mk({ asset: 'MARKET' }))).toBe(false);
  });

  it('returns false when AI analysis is null (AI failed)', () => {
    expect(isExecutableSignal(mk({ ai_analysis: null }))).toBe(false);
  });

  it('returns false when direction is wait', () => {
    expect(
      isExecutableSignal(mk({ ai_analysis: { suggested_action: 'wait' } })),
    ).toBe(false);
  });
});

describe('signalExecuteHref', () => {
  it('builds the canonical /execute?signal_id=N URL', () => {
    expect(signalExecuteHref(42)).toBe('/execute?signal_id=42');
  });
});

describe('sideForSuggestedAction', () => {
  it.each<[SuggestedAction, Side | null]>([
    ['consider long', 'buy'],
    ['consider short', 'sell'],
    ['wait', null],
  ])('%s → %s', (action, expected) => {
    expect(sideForSuggestedAction(action)).toBe(expected);
  });
});

describe('defaultVenueForSuggestedAction', () => {
  it.each<[SuggestedAction, Venue | null]>([
    ['consider long', 'sodex_spot'],
    ['consider short', 'sodex_perps'],
    ['wait', null],
  ])('%s → %s', (action, expected) => {
    expect(defaultVenueForSuggestedAction(action)).toBe(expected);
  });
});
