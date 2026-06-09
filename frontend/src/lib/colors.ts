/**
 * Data-driven color resolvers for the redesign (R0).
 *
 * Each returns a `ColorToken` (a `--token` name) so callers compose with
 * `cssVar()` (solid: SVG stroke/fill, text) or `colorMix()` (tinted fills) from
 * `./colorMix`. Returning the TOKEN — not a baked `var(--x)` string — keeps one
 * source of truth and lets the exact design percentages live in `colorMix`.
 *
 * These mirror the prototype's `assetColor / confColor / confSoft / actionMeta /
 * ciTone` and the detector/regime color identity, with the new design tokens.
 * (Legacy `confidenceColor / regimeColor / postureColor` in `./format` stay for
 * not-yet-ported screens and are removed at R10.)
 */

import type { AssetSymbol, MarketRegime, SignalType, SuggestedAction } from '../api/types';
import type { ColorToken } from './colorMix';

/** Detector identity color (badge, icon, leaderboard, signal accent). */
const DETECTOR_COLOR: Record<SignalType, ColorToken> = {
  flow_anomaly: '--det-flow',
  magnitude: '--det-mag',
  acceleration: '--det-accel',
  divergence: '--det-div',
  regime_shift: '--det-regime',
};
export function detectorColorToken(type: SignalType): ColorToken {
  return DETECTOR_COLOR[type];
}

/** Wyckoff regime state color. */
const REGIME_COLOR: Record<MarketRegime, ColorToken> = {
  accumulation: '--reg-accum',
  markup: '--reg-markup',
  distribution: '--reg-dist',
  markdown: '--reg-markdown',
  uncertain: '--reg-uncertain',
};
export function regimeColorToken(regime: MarketRegime): ColorToken {
  return REGIME_COLOR[regime];
}

/** Asset brand color. */
const ASSET_COLOR: Record<AssetSymbol, ColorToken> = {
  BTC: '--btc',
  ETH: '--eth',
  MARKET: '--market',
};
export function assetColorToken(asset: AssetSymbol): ColorToken {
  return ASSET_COLOR[asset];
}

/** Type guard — is this arbitrary string one of our branded assets? */
export function isKnownAsset(asset: string): asset is AssetSymbol {
  return asset in ASSET_COLOR;
}

/**
 * Brand token for an arbitrary asset string, or `null` when unbranded.
 *
 * Execution orders/positions carry venue-symbol forms like `"BTC-USD"`
 * (not just the bare `"BTC"` the detectors emit), so a plain
 * `assetColorToken` lookup misses and the chip ends up with no fill +
 * near-black `--ink` text (unreadable). We resolve those by their leading
 * base symbol (`"BTC-USD"` / `"BTC/USDT"` → BTC orange) and only fall back
 * to the neutral chip for a genuinely unknown ticker.
 */
export function assetBrandToken(asset: string): ColorToken | null {
  if (isKnownAsset(asset)) return ASSET_COLOR[asset];
  const base = asset.split(/[-/]/)[0]?.toUpperCase();
  if (base && isKnownAsset(base)) return ASSET_COLOR[base];
  return null;
}

/** Confidence 1–10 → color tier (4-step ramp, matching the prototype). */
export function confColorToken(c: number): ColorToken {
  if (c >= 8) return '--win';
  if (c >= 6) return '--conf-mid';
  if (c >= 4) return '--warn';
  return '--loss';
}

/** Confidence 1–10 → soft (tinted background) tier. */
export function confSoftToken(c: number): ColorToken {
  if (c >= 8) return '--win-soft';
  if (c >= 4) return '--warn-soft';
  return '--loss-soft';
}

/** Wilson-CI tone for a leaderboard/calibration cell: green if the lower bound
 *  clears 0.5 (statistically above noise), red if the upper bound is below 0.5,
 *  neutral otherwise. Null bounds → neutral. */
export function ciToneToken(
  ciLow: number | null | undefined,
  ciHigh: number | null | undefined,
): ColorToken {
  if (ciLow != null && ciLow > 0.5) return '--win';
  if (ciHigh != null && ciHigh < 0.5) return '--loss';
  return '--warn';
}

/** Suggested-action display metadata (color tier, terse + full label, glyph). */
export interface ActionMeta {
  tone: ColorToken;
  label: string;
  full: string;
  arrow: string;
}
export function actionMeta(action: SuggestedAction): ActionMeta {
  switch (action) {
    case 'consider long':
      return { tone: '--win', label: 'Long', full: 'Consider long', arrow: '▲' };
    case 'consider short':
      return { tone: '--loss', label: 'Short', full: 'Consider short', arrow: '▼' };
    case 'wait':
      return { tone: '--warn', label: 'Wait', full: 'Wait', arrow: '■' };
  }
}
