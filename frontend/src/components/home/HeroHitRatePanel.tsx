import { StatusDot } from '../ui';

/**
 * The oversized hit-rate card that anchors the Data-forward (HomeV3) hero.
 *
 * Layout (per mock HomeV3):
 *   ┌─────────────────────────────────────────┐
 *   │ 72H HIT RATE              ● tracking    │  <- bg-3 header strip, mono 10px
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
 * Hit-rate data isn't available until Stage 8 (SignalOutcome). For now the
 * big number renders "—" with a muted "Evaluation pending (Stage 08)"
 * caption. Structure stays intact; when outcome rows arrive, the backend
 * adds `hit_rate_72h` + `evaluated_count` to DashboardStats and this
 * component swaps placeholders for real values — no layout change.
 */

interface HeroHitRatePanelProps {
  /** null = stats fetch errored (render "—" rather than a misleading 0). */
  signalsToday: number | null;
  totalSignals: number | null;
  /** Optional — null until Stage 08 plumbs it through. */
  hitRate72h?: number | null;
  /** Optional — null until Stage 08. */
  evaluatedCount?: number | null;
}

export function HeroHitRatePanel({
  signalsToday,
  totalSignals,
  hitRate72h = null,
  evaluatedCount = null,
}: HeroHitRatePanelProps) {
  const hasHitRate = hitRate72h !== null && hitRate72h !== undefined;
  const pctString = hasHitRate ? Math.round(hitRate72h! * 100).toString() : '—';

  return (
    <div className="border border-border-2 rounded-[10px] bg-bg-2 overflow-hidden">
      {/* Header strip */}
      <div className="flex items-center justify-between px-4 py-2.5 bg-bg-3 border-b border-border-2 font-mono text-[10px] uppercase tracking-[0.1em] text-text-3">
        <span>72H HIT RATE</span>
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
            : 'Evaluation pending (Stage 08 — signal outcomes)'}
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
