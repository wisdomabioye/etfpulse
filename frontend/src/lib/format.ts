/**
 * Small display-formatting helpers.
 *
 * These are intentionally trivial — no date-fns, no moment. For 4-5
 * formatters on the hot path, hand-rolled is smaller than any dep.
 */

import type { SignalType } from '../api/types';

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
