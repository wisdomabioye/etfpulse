/**
 * Small display-formatting helpers.
 *
 * These are intentionally trivial — no date-fns, no moment. For 4-5
 * formatters on the hot path, hand-rolled is smaller than any dep.
 */

import type { MarketRegime, SignalPosture, SignalType } from '../api/types';

/** Human-readable signal type: "flow_anomaly" → "Flow Anomaly". */
export function formatSignalType(type: SignalType): string {
  return type
    .split('_')
    .map((word) => word[0].toUpperCase() + word.slice(1))
    .join(' ');
}

/** ISO datetime → "2h ago" / "3d ago" / "just now".
 *
 * Coarse-grained — good enough for a signal feed. Switch to Intl.RelativeTimeFormat
 * if we ever want localization. */
export function formatAgo(iso: string): string {
  const now = Date.now();
  const then = new Date(iso).getTime();
  if (isNaN(then)) return '—';
  const diffSec = Math.floor((now - then) / 1000);
  if (diffSec < 60) return 'just now';
  if (diffSec < 3600) return `${Math.floor(diffSec / 60)}m ago`;
  if (diffSec < 86400) return `${Math.floor(diffSec / 3600)}h ago`;
  const days = Math.floor(diffSec / 86400);
  if (days < 30) return `${days}d ago`;
  if (days < 365) return `${Math.floor(days / 30)}mo ago`;
  return `${Math.floor(days / 365)}y ago`;
}

/** Traffic-light color token for a confidence value. NULL → muted.
 *
 * Matches mock's convColor():
 *   confidence ≥ 7 → --pos (green)
 *   4 ≤ conf ≤ 6   → --warn (amber)
 *   conf < 4       → --neg (red)
 */
export function confidenceColor(c: number | null | undefined): string {
  if (c == null) return 'var(--color-text-4)';
  if (c >= 7) return 'var(--color-pos)';
  if (c >= 4) return 'var(--color-warn)';
  return 'var(--color-neg)';
}

/** Short fingerprint for display — 8 chars of the full 32-char SHA-256 prefix.
 * Decision locked in Stage 6 planning: frontend truncates, backend returns full. */
export function truncateFingerprint(fp: string): string {
  return fp.slice(0, 8);
}

/** Plain USD price formatter — `$84,200.50`. Up to 2 decimals, locale-grouped.
 *  Use this for spot prices, entry/stop/target levels, +24h/+72h prices.
 *
 *  NOT to be confused with `RegimeReasoning`'s compact `$X.XB` formatter,
 *  which takes a Decimal-as-string and emits magnitude suffixes for the
 *  flow-trend-millions/billions display. Different consumer, different
 *  signature, different module. */
export function formatUsdPrice(n: number): string {
  return `$${n.toLocaleString('en-US', { maximumFractionDigits: 2 })}`;
}

/** Title-case a single Wyckoff regime: "accumulation" → "Accumulation".
 * Mirrors `formatSignalType` styling — no shared helper since regime values
 * never carry an underscore (single-word enum). */
export function formatRegime(r: MarketRegime): string {
  return r[0].toUpperCase() + r.slice(1);
}

/** Title-case a single posture value, matching `formatRegime` styling. */
export function formatPosture(p: SignalPosture): string {
  return p[0].toUpperCase() + p.slice(1);
}

/** Color token for a Wyckoff regime. Mirrors classifier semantics:
 *   accumulation → pos (bullish base building)
 *   markup       → pos (active uptrend)
 *   distribution → warn (topping)
 *   markdown     → neg (active downtrend)
 *   uncertain    → muted (no high-confidence read)
 *
 * Keep in sync with `pipeline/regime_monitor.py`'s MarketRegime enum;
 * any new regime value must extend both this map AND the MarketRegime
 * union in `api/types.ts`. */
export function regimeColor(r: MarketRegime): string {
  switch (r) {
    case 'accumulation':
    case 'markup':
      return 'var(--color-pos)';
    case 'distribution':
      return 'var(--color-warn)';
    case 'markdown':
      return 'var(--color-neg)';
    case 'uncertain':
      return 'var(--color-text-4)';
  }
}

/** Color token for a posture: how aggressively the engine should fire.
 *   aggressive → pos     (lower bar for new signals)
 *   normal     → info    (default operating mode)
 *   cautious   → warn    (raise the bar; macro/news risk elevated)
 *   paused     → neg     (no new signals fanned out)
 *
 * Used by the home regime tile + RegimeBadge in TopNav. Keep parallel
 * to `regimeColor` so both helpers stay together when extending. */
export function postureColor(p: SignalPosture): string {
  switch (p) {
    case 'aggressive':
      return 'var(--color-pos)';
    case 'normal':
      return 'var(--color-info)';
    case 'cautious':
      return 'var(--color-warn)';
    case 'paused':
      return 'var(--color-neg)';
  }
}

// ---------------------------------------------------------------------------
// Enum narrowers — `DashboardStats.current_regime` and `signal_posture` are
// `string | null` (Pydantic widens to str at the JSON boundary, even though
// the column is CHECK-constrained). Both consumers (TopNav, Home) need to
// safely narrow before passing to the strictly-typed badge components.
// One helper each so we never drift on which values are valid.
// ---------------------------------------------------------------------------

const REGIME_VALUES: ReadonlySet<string> = new Set<MarketRegime>([
  'accumulation',
  'markup',
  'distribution',
  'markdown',
  'uncertain',
]);

const POSTURE_VALUES: ReadonlySet<string> = new Set<SignalPosture>([
  'aggressive',
  'normal',
  'cautious',
  'paused',
]);

/** Narrow an unknown-shaped string into `MarketRegime` or null. Returns
 *  null on null/undefined input AND on any string outside the enum —
 *  defensive against a future backend stamping a value that frontend
 *  hasn't been redeployed for. Callers render a "not yet classified"
 *  fallback rather than crashing. */
export function narrowRegime(value: string | null | undefined): MarketRegime | null {
  return value !== null && value !== undefined && REGIME_VALUES.has(value)
    ? (value as MarketRegime)
    : null;
}

/** Sibling of `narrowRegime` for `SignalPosture`. Same semantics. */
export function narrowPosture(value: string | null | undefined): SignalPosture | null {
  return value !== null && value !== undefined && POSTURE_VALUES.has(value)
    ? (value as SignalPosture)
    : null;
}
