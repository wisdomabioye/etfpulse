import type { AIAnalysis } from '../../api/types';
import { formatUsdPrice } from '../../lib/format';

interface SuggestedActionPanelProps {
  analysis: AIAnalysis;
  /** Narrow layout: 2-col grid, skip Entry/Stop/Target. */
  compact?: boolean;
}

/**
 * The "Suggested action" panel on signal detail. Color-tinted card keyed
 * on the action direction — pos (long), neg (short), info (wait). 3-col
 * grid on desktop: Direction · Horizon · Entry/Stop/Target.
 *
 * Stage 8-P7 — entry/stop/target now render live from `analysis.entry_price`
 * etc. (P1 added these fields to the AI response). When the AI declined
 * to volunteer them OR when the suggested action is "wait" (validator
 * drops prices in that case), the field reads "—" rather than dropping
 * the slot — keeps the 3-col grid stable across signals.
 *
 * Footer disclaimer is intentional and must not be removed — this is the
 * only surface where we show an action recommendation, and the copy is
 * part of the UX contract.
 */
export function SuggestedActionPanel({ analysis, compact = false }: SuggestedActionPanelProps) {
  const action = analysis.suggested_action;
  const tone: 'pos' | 'neg' | 'info' = action.includes('long')
    ? 'pos'
    : action.includes('short')
      ? 'neg'
      : 'info';
  const color = `var(--color-${tone})`;

  // R:R derived inline — only meaningful when ALL THREE levels are set
  // AND the direction implies a side (skip on "wait"). The math is
  // unsigned magnitude / unsigned magnitude — caller doesn't need to know
  // which way is "good".
  const rr = computeRiskReward(analysis);

  return (
    <div
      className="rounded-[10px]"
      style={{
        border: `1px solid color-mix(in oklab, ${color} 30%, transparent)`,
        background: `color-mix(in oklab, ${color} 6%, var(--bg-2))`,
        borderLeft: `3px solid ${color}`,
        padding: compact ? 16 : 22,
      }}
    >
      <div className={`grid gap-4 ${compact ? 'grid-cols-2' : 'grid-cols-2 md:grid-cols-3'}`}>
        <Field label="Direction">
          <span className="font-semibold capitalize" style={{ fontSize: 18, color }}>
            {action}
          </span>
        </Field>
        <Field label="Horizon">
          <span className="text-[18px] font-semibold capitalize text-text-1">
            {analysis.time_horizon}
          </span>
        </Field>
        {!compact && (
          <Field label={rr !== null ? `Entry / Stop / Target · R:R 1:${rr}` : 'Entry / Stop / Target'}>
            <PriceTriple
              entry={analysis.entry_price}
              stop={analysis.stop_price}
              target={analysis.target_price}
            />
          </Field>
        )}
      </div>
      <div className="mt-3.5 pt-3.5 border-t border-border-2 font-mono text-[10px] text-text-3 tracking-[0.05em]">
        Not financial advice. Validate before trading.
      </div>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="font-mono text-[10px] text-text-3 uppercase tracking-[0.1em] mb-1.5">
        {label}
      </div>
      {children}
    </div>
  );
}

function PriceTriple({
  entry,
  stop,
  target,
}: {
  entry: number | null;
  stop: number | null;
  target: number | null;
}) {
  // All-null → AI declined OR action is "wait". One muted dash beats three
  // empty cells.
  if (entry === null && stop === null && target === null) {
    return <span className="font-mono text-[13px] text-text-3">—</span>;
  }
  return (
    <div className="font-mono text-[13px] text-text-1 tabular-nums leading-snug">
      <div>{entry !== null ? formatUsdPrice(entry) : '—'}</div>
      <div className="text-text-3 text-[11px]">
        stop {stop !== null ? formatUsdPrice(stop) : '—'} · target{' '}
        {target !== null ? formatUsdPrice(target) : '—'}
      </div>
    </div>
  );
}

/** Risk:reward as `target ÷ stop` distance (rounded to 1 decimal). Returns
 *  null if any leg is missing OR if direction is wait. Magnitude only —
 *  the caller renders "1:N" so the convention is uniform across long/short. */
function computeRiskReward(analysis: AIAnalysis): number | null {
  if (analysis.suggested_action === 'wait') return null;
  const { entry_price: entry, stop_price: stop, target_price: target } = analysis;
  if (entry === null || stop === null || target === null) return null;
  const risk = Math.abs(entry - stop);
  const reward = Math.abs(target - entry);
  if (risk === 0) return null; // would divide by zero — bad data, hide it
  return Math.round((reward / risk) * 10) / 10;
}

