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
 *  NOT to be confused with `formatCompactFlow` below, which emits magnitude
 *  suffixes (`$X.XB`) for the millions/billions flow display. */
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

// ---------------------------------------------------------------------------
// Redesign (R0) — numeric formatters ported from the prototype's `fmt.*`.
// All accept `number | null | undefined` and emit an em-dash for the empty
// state (the prototype's universal "—" convention) so callers never guard.
// `formatAgo` above already covers the prototype's `fmt.rel` — reuse it.
// ---------------------------------------------------------------------------

type Num = number | null | undefined;

/** Price with magnitude-scaled decimals: ≥1000 → 0dp, ≥1 → 2dp, else 4dp.
 *  Pass `dp` to override (e.g. a fixed-2dp column). Grouped, USD-prefixed. */
export function formatPrice(v: Num, dp?: number): string {
  if (v == null) return '—';
  const decimals = dp ?? (v >= 1000 ? 0 : v >= 1 ? 2 : 4);
  return `$${v.toLocaleString('en-US', {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  })}`;
}

/**
 * Trim trailing zeros from a fixed-precision decimal string — the shape
 * Postgres `NUMERIC` columns serialize to over JSON (e.g. order size
 * `"0.002000000000000000"`, price `"65000.00000000"`). String-based so a
 * float round-trip can't drop precision on large quantities. `null`/empty
 * → em dash.
 *
 *   "0.002000000000000000" → "0.002"
 *   "65000.00000000"       → "65000"
 *   "3"                    → "3"
 *   "1.50"                 → "1.5"
 */
export function trimDecimal(v: string | number | null | undefined): string {
  if (v == null || v === '') return '—';
  const s = String(v);
  if (!s.includes('.')) return s;
  return s.replace(/(\.\d*?)0+$/, '$1').replace(/\.$/, '');
}

/** Compact signed flow, input already in MILLIONS: 1500 → "+$1.5B", -300 → "-$300M". */
export function formatCompactFlow(v: Num): string {
  if (v == null) return '—';
  const abs = Math.abs(v);
  const sign = v < 0 ? '-' : '+';
  if (abs >= 1000) return `${sign}$${(abs / 1000).toFixed(1)}B`;
  return `${sign}$${abs.toFixed(0)}M`;
}

/** Fraction → percent: 0.42 → "42.0%". `dp` controls decimals (default 1). */
export function formatPct(v: Num, dp = 1): string {
  if (v == null) return '—';
  return `${(v * 100).toFixed(dp)}%`;
}

/** Already-a-percent value → percent string: 42 → "42.0%". (No ×100.) */
export function formatPctRaw(v: Num, dp = 1): string {
  if (v == null) return '—';
  return `${v.toFixed(dp)}%`;
}

/** Signed value with a forced leading "+": 2.3 → "+2.30%", -1 → "-1.00%".
 *  `suffix` defaults to "%"; pass "" for a bare signed number. */
export function formatSignedPct(v: Num, dp = 2, suffix = '%'): string {
  if (v == null) return '—';
  return `${v >= 0 ? '+' : ''}${v.toFixed(dp)}${suffix}`;
}

/** Confidence as "X/10". Rounds to at most 1 decimal so an averaged value
 *  (e.g. `3.6153846…`) renders as "3.6/10", while an integer stays clean
 *  ("8/10", not "8.0/10"). Null → "—". */
export function formatConfidence(v: Num): string {
  if (v == null) return '—';
  return `${Number(v.toFixed(1))}/10`;
}

/** Locale-grouped integer/number. Null → "—". */
export function formatNumber(v: Num): string {
  if (v == null) return '—';
  return v.toLocaleString('en-US');
}

/** Hours from now until `iso` (negative if already past). Null on a bad
 *  date. Reads the clock here (a plain fn) so callers stay render-pure. */
export function hoursUntil(iso: string): number | null {
  const t = new Date(iso).getTime();
  if (isNaN(t)) return null;
  return (t - Date.now()) / 3_600_000;
}

/** Human "remaining until evaluation" string from an hours-remaining value.
 *  Precise: hours under 2 days, `d h` beyond. Past → "evaluation due". */
export function formatRemaining(hours: number): string {
  if (hours <= 0) return 'evaluation due';
  if (hours < 48) return `~${Math.round(hours)}h remaining`;
  const d = Math.floor(hours / 24);
  const h = Math.round(hours - d * 24);
  return `~${d}d ${h}h remaining`;
}

/** True when `iso` is within the last `ms` milliseconds. Reads the clock
 *  here (a plain function) rather than in a component render, where
 *  `Date.now()` would trip the react-hooks purity rule — same reason
 *  `formatAgo` reads the clock in this module, not at the call site. */
export function isWithin(iso: string, ms: number): boolean {
  const then = new Date(iso).getTime();
  if (isNaN(then)) return false;
  return Date.now() - then < ms;
}

/** Absolute UTC timestamp for detail views: ISO → "Mon, 08 Jun 2026 04:30:00 UTC". */
export function formatAbsUtc(iso: string): string {
  const ms = new Date(iso).getTime();
  if (isNaN(ms)) return iso;
  return new Date(ms).toUTCString().replace('GMT', 'UTC');
}
