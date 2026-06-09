import type { SignalOutcome } from '../../api/types';
import { formatUsdPrice } from '../../lib/format';

// ---------------------------------------------------------------------------
// Verdict picker — single source of truth for outcome tone + label.
// ---------------------------------------------------------------------------

export interface Verdict {
  label: string;
  color: string;
}

export function pickVerdict(o: SignalOutcome): Verdict {
  if (o.hit_target === true) return { label: '✓ Target hit', color: 'var(--color-pos)' };
  if (o.hit_stop === true) return { label: '✗ Stop hit', color: 'var(--color-neg)' };
  if (o.hit_target === null) return { label: '— No target set', color: 'var(--color-text-4)' };
  return { label: '— Neither hit', color: 'var(--color-text-3)' };
}

// ---------------------------------------------------------------------------
// Formatters
// ---------------------------------------------------------------------------

export interface ReturnPct {
  text: string;
  colorClass: string;
}

/** Computes `(price - baseline) / baseline * 100`, formatted with sign +
 *  tone class. Null when either input is null OR baseline is non-positive
 *  (would divide by zero — same defensive guard as TrackRecord page row). */
export function pctReturn(price: number | null, baseline: number | null): ReturnPct | null {
  if (price === null || baseline === null || baseline <= 0) return null;
  const pct = ((price - baseline) / baseline) * 100;
  return {
    text: `${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%`,
    colorClass: pct >= 0 ? 'text-pos' : 'text-neg',
  };
}

export function fmtPriceOrDash(n: number | null): string {
  return n !== null ? formatUsdPrice(n) : '—';
}

export function formatCountdown(iso: string): string | null {
  const target = new Date(iso).getTime();
  if (isNaN(target)) return null;
  const diffMs = target - Date.now();
  if (diffMs <= 0) return 'due now';
  const hours = Math.floor(diffMs / 3_600_000);
  if (hours < 1) return '<1h';
  if (hours < 48) return `${hours}h`;
  const days = Math.floor(hours / 24);
  return `${days}d`;
}
