import { useLiveness } from '../../api/queries';
import { StatusDot } from '../ui/StatusDot';

/** Static dot + an outward-pinging ring behind it. The ring is absolutely
 *  positioned so it never affects layout, and uses Tailwind's `animate-ping`
 *  (scale+fade 1s loop). Color comes from the same CSS var the dot uses so
 *  both stay in lockstep when we change tone. */
function PulsingDot({ color }: { color: 'pos' | 'warn' | 'neg' }) {
  const VAR: Record<typeof color, string> = {
    pos: 'var(--color-pos)',
    warn: 'var(--color-warn)',
    neg: 'var(--color-neg)',
  };
  return (
    <span className="relative inline-flex w-1.5 h-1.5">
      <span
        className="absolute inset-0 rounded-full animate-ping opacity-75"
        style={{ background: VAR[color] }}
        aria-hidden
      />
      <StatusDot color={color} size="sm" glow />
    </span>
  );
}

/**
 * Liveness indicator pinned to the TopNav right edge.
 *
 * Three states map to `/api/health/ready` outcomes:
 * - `live` (green, glowing)    — 200, no config errors/warnings
 * - `degraded` (amber, glowing) — 200 with warnings, OR 503 with config errors but DB fine
 * - `down` (red, glowing)       — 503 with `db: error`, or network/CORS failure
 *
 * The label is a `title` attribute (tooltip) carrying the first config
 * error/warning string when present — operators can hover to see *why*
 * we're degraded without opening DevTools. Pure presentation; the polling
 * and state derivation live in `useLiveness`.
 */
export function LivePulse() {
  const { data, isLoading } = useLiveness();

  if (isLoading || !data) {
    return (
      <span className="inline-flex items-center gap-1.5 text-[12px] text-text-4 select-none">
        <StatusDot color="muted" size="sm" hollow />
        <span>checking</span>
      </span>
    );
  }

  const { state, readiness } = data;

  const COPY: Record<typeof state, { label: string; color: 'pos' | 'warn' | 'neg' }> = {
    live: { label: 'Live', color: 'pos' },
    degraded: { label: 'Degraded', color: 'warn' },
    down: { label: 'Down', color: 'neg' },
  };
  const { label, color } = COPY[state];

  // Tooltip prefers the first hard error, then the first warning, then a
  // generic state description. Keeps the visible label terse while still
  // surfacing the actionable detail on hover.
  const detail =
    readiness?.config.errors[0] ??
    readiness?.config.warnings[0] ??
    (state === 'down'
      ? 'API unreachable'
      : state === 'degraded'
        ? 'service reachable, config issues'
        : 'all systems operational');

  return (
    <span
      className="inline-flex items-center gap-1.5 text-[12px] text-text-3 select-none"
      title={detail}
      aria-label={`Service status: ${label}. ${detail}`}
    >
      <PulsingDot color={color} />
      <span>{label}</span>
    </span>
  );
}
