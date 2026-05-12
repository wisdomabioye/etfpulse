import { StatusDot } from '../ui';

/**
 * The oversized hit-rate card that anchors the Data-forward (HomeV3) hero.
 *
 * Layout (per mock HomeV3):
 *   ┌─────────────────────────────────────────┐
 *   │ HIT RATE                  ● tracking    │  <- bg-3 header strip, mono 10px
 *   ├─────────────────────────────────────────┤
 *   │                                         │
 *   │  68%                                    │  <- 96px, tabular, `%` is text-3 + 48px
 *   │  on 138 evaluated signals               │  <- text-2 caption
 *   │                                         │
 *   │  ┌─────────────┬─────────────┐          │
 *   │  │ SIGNALS     │ TOTAL       │          │  <- mono 10px labels
 *   │  │ TODAY       │             │          │
 *   │  │ 2           │ 156         │          │  <- 24px
 *   │  └─────────────┴─────────────┘          │
 *   └─────────────────────────────────────────┘
 *
 * PR B (#60) — relabeled from "72H HIT RATE" to "HIT RATE" because the
 * v2 rubric scores each signal against its OWN validity window (scalp 6h /
 * swing 72h / position 168h), not a fixed 72h. The headline number is the
 * mixed-window global hit rate; the bucketed comparison lives on the
 * TrackRecord page.
 *
 * `hitRateGlobal` prop is in PERCENT (0..100) — same unit as the API
 * field (`DashboardStats.hit_rate_global`) and
 * `/api/track-record.summary.hit_rate_pct`. One canonical unit across the
 * stack means the panel doesn't multiply or divide; the API number renders
 * verbatim.
 */

interface HeroHitRatePanelProps {
  /** null = stats fetch errored (render "—" rather than a misleading 0). */
  signalsToday: number | null;
  totalSignals: number | null;
  /** Hit rate as PERCENT (0..100). Null when no signal with a target has
   *  been scored yet — caption swaps to the pending state. */
  hitRateGlobal?: number | null;
  /** Total evaluated outcome rows. 0 before any signal ages past its
   *  validity window. */
  evaluatedCount?: number | null;
}

export function HeroHitRatePanel({
  signalsToday,
  totalSignals,
  hitRateGlobal = null,
  evaluatedCount = null,
}: HeroHitRatePanelProps) {
  const hasHitRate = hitRateGlobal !== null && hitRateGlobal !== undefined;
  // Already in percent — render as integer for the headline, no `* 100`
  // conversion (the API serialises percent, not fraction).
  const pctString = hasHitRate ? Math.round(hitRateGlobal!).toString() : '—';

  return (
    <div className="border border-border-2 rounded-[10px] bg-bg-2 overflow-hidden">
      {/* Header strip */}
      <div className="flex items-center justify-between px-4 py-2.5 bg-bg-3 border-b border-border-2 font-mono text-[10px] uppercase tracking-[0.1em] text-text-3">
        <span>HIT RATE</span>
        <span className={`inline-flex items-center gap-1.5 ${hasHitRate ? 'text-pos' : 'text-text-4'}`}>
          <StatusDot color={hasHitRate ? 'pos' : 'muted'} hollow={!hasHitRate} />
          {hasHitRate ? 'tracking' : 'pending'}
        </span>
      </div>

      {/* Body */}
      <div className="px-7 pt-8 pb-5">
        <div
          className="text-text-1 tabular-nums font-semibold leading-none"
          style={{ fontSize: 96, letterSpacing: '-0.05em' }}
        >
          {pctString}
          {hasHitRate && (
            <span className="text-text-3" style={{ fontSize: 48 }}>
              %
            </span>
          )}
        </div>
        <div className="text-text-2 text-[13px] mt-2 mb-6">
          {hasHitRate
            ? `on ${evaluatedCount ?? 0} evaluated signals`
            : 'Evaluation pending — first outcomes land once signals complete their validity window'}
        </div>

        {/* 2-col split: signals today + total */}
        <div className="grid grid-cols-2 border-t border-border-2">
          <div className="py-4 border-r border-border-2">
            <div className="font-mono text-[10px] text-text-3 uppercase tracking-[0.1em] mb-1.5">
              Signals today
            </div>
            <div className="text-2xl font-semibold tabular-nums text-text-1">
              {signalsToday ?? '—'}
            </div>
          </div>
          <div className="py-4 pl-[18px]">
            <div className="font-mono text-[10px] text-text-3 uppercase tracking-[0.1em] mb-1.5">
              Total
            </div>
            <div className="text-2xl font-semibold tabular-nums text-text-1">
              {totalSignals ?? '—'}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
