/**
 * Runtime constants for the asset + signal-type domain enums (issue #75).
 *
 * `as const` freezes the literal types so the derived unions match the
 * runtime values exactly — no drift between "the type says BTC|ETH" and
 * "the dropdown shows BTC|ETH|SOL." Adding a new asset or detector is a
 * one-line change here; every consumer that maps over these arrays picks
 * it up automatically.
 *
 * Type unions are re-exported from `api/types.ts` to keep import paths
 * stable for existing consumers. Use those for type annotations;
 * import the arrays from this module when you need the runtime list.
 */

// MARKET is the cross-asset sentinel (PR F.3 — regime_shift fires once
// per UTC-day transition against the whole market, not per asset). It
// renders as a neutral chip in AssetBadge and appears as a third pill
// in the FilterBar so users can isolate market-wide tags.
export const ASSETS = ['BTC', 'ETH', 'MARKET'] as const;

export const SIGNAL_TYPES = [
  'flow_anomaly',
  'magnitude',
  'acceleration',
  'divergence',
  'regime_shift',
] as const;

export type AssetSymbol = (typeof ASSETS)[number];
export type SignalType = (typeof SIGNAL_TYPES)[number];
