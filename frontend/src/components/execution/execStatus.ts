/**
 * Order-status helpers shared by the orders + close flows.
 *
 *   - CANCELABLE_STATUSES  — statuses where a Cancel button is shown.
 *   - NON_TERMINAL_STATUSES — "open" set (drives the Orders "open" filter).
 *   - orderStatusMeta       — status → StatusDot color + label + pulse flag.
 */

// Statuses that can be cancelled. Anything else either has no venue presence
// yet (PENDING is a local cancel) or is already terminal.
export const CANCELABLE_STATUSES = new Set(['pending', 'acked', 'partially_filled']);

export const NON_TERMINAL_STATUSES = new Set([
  'pending',
  'submitted',
  'acked',
  'partially_filled',
]);

export const ORDER_FILTERS = ['all', 'open', 'filled', 'rejected'] as const;
export type OrderFilter = (typeof ORDER_FILTERS)[number];

export function matchesOrderFilter(status: string, f: OrderFilter): boolean {
  if (f === 'all') return true;
  if (f === 'open') return NON_TERMINAL_STATUSES.has(status);
  return status === f;
}

// Mirrors the `StatusDot` color union (components/ui/StatusDot).
export type DotColor = 'accent' | 'pos' | 'neg' | 'warn' | 'info' | 'muted';

export function orderStatusMeta(status: string): {
  color: DotColor;
  label: string;
  pulse: boolean;
} {
  switch (status) {
    case 'pending':
      return { color: 'warn', label: 'pending', pulse: false };
    case 'submitted':
      return { color: 'info', label: 'submitted', pulse: true };
    case 'acked':
      return { color: 'info', label: 'acked', pulse: true };
    case 'partially_filled':
      return { color: 'info', label: 'partial', pulse: true };
    case 'filled':
      return { color: 'pos', label: 'filled', pulse: false };
    case 'rejected':
      return { color: 'neg', label: 'rejected', pulse: false };
    case 'cancelled':
      return { color: 'muted', label: 'cancelled', pulse: false };
    case 'expired':
      return { color: 'muted', label: 'expired', pulse: false };
    default:
      return { color: 'muted', label: status, pulse: false };
  }
}
