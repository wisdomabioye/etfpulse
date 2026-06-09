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

// `import type` from api/types is erased at compile time, so the
// constants→api/types→constants edge is a TYPE-only cycle (no runtime cycle).
import type { ColorToken } from './colorMix';
import type { MarketRegime, TimeHorizon } from '../api/types';

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

// ---------------------------------------------------------------------------
// Redesign (R0) — display config keyed off the domain enums above.
// Colors are NOT stored here: a detector's/regime's color comes from
// `colors.ts` (single source of truth), so this stays pure labels/metadata.
// ---------------------------------------------------------------------------

/** Per-detector display metadata (label, terse label, one-line "what it catches"). */
export const DETECTORS: Record<SignalType, { label: string; short: string; catches: string }> = {
  flow_anomaly: { label: 'Flow Anomaly', short: 'Flow', catches: 'N-day streak breaks in net flow' },
  magnitude: { label: 'Magnitude', short: 'Mag', catches: 'Z-score / percentile flow outliers' },
  acceleration: {
    label: 'Acceleration',
    short: 'Accel',
    catches: 'Multi-window 2nd-derivative shifts',
  },
  divergence: {
    label: 'Divergence',
    short: 'Div',
    catches: 'Institutional-vs-retail / BTC-vs-ETH splits',
  },
  regime_shift: { label: 'Regime Shift', short: 'Regime', catches: 'Market-state transitions' },
};

/** Per-regime display metadata (label + glyph; color via `regimeColorToken`). */
export const REGIMES: Record<MarketRegime, { label: string; glyph: string }> = {
  accumulation: { label: 'Accumulation', glyph: '▟' },
  markup: { label: 'Markup', glyph: '▲' },
  distribution: { label: 'Distribution', glyph: '▤' },
  markdown: { label: 'Markdown', glyph: '▼' },
  uncertain: { label: 'Uncertain', glyph: '╳' },
};

/** Order lifecycle statuses (one display vocabulary across orders + history). */
export const ORDER_STATUSES = [
  'pending',
  'submitted',
  'acked',
  'partially_filled',
  'filled',
  'cancelled',
  'rejected',
  'expired',
] as const;
export type OrderStatus = (typeof ORDER_STATUSES)[number];

/** Per-status display metadata (label + tone token). */
export const ORDER_STATUS: Record<OrderStatus, { label: string; tone: ColorToken }> = {
  pending: { label: 'Pending', tone: '--t3' },
  submitted: { label: 'Submitted', tone: '--info' },
  acked: { label: 'Acked', tone: '--info' },
  partially_filled: { label: 'Partial Fill', tone: '--warn' },
  filled: { label: 'Filled', tone: '--win' },
  cancelled: { label: 'Cancelled', tone: '--t3' },
  rejected: { label: 'Rejected', tone: '--loss' },
  expired: { label: 'Expired', tone: '--t4' },
};

/** Horizon buckets (label + human window) for the calibration / track-record tabs. */
export const HORIZONS: ReadonlyArray<{ key: TimeHorizon; label: string; window: string }> = [
  { key: 'scalp', label: 'Scalp', window: '≤24h' },
  { key: 'swing', label: 'Swing', window: '24–72h' },
  { key: 'position', label: 'Position', window: '>72h' },
];

/** Confidence-bucket labels for the calibration curve x-axis (default 2-wide buckets). */
export const CONF_BUCKETS = ['1–2', '3–4', '5–6', '7–8', '9–10'] as const;

/** Ideal hit-rate midpoint per confidence bucket (the calibration diagonal). */
export const BUCKET_IDEAL = [0.15, 0.35, 0.55, 0.75, 0.95] as const;
