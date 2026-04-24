import type { AIAnalysis } from '../../api/types';

interface SuggestedActionPanelProps {
  analysis: AIAnalysis;
  /** Narrow layout: 2-col grid, skip Entry/Stop/Target. */
  compact?: boolean;
}

/**
 * The "Suggested action" panel on signal detail. Color-tinted card keyed
 * on the action direction — pos (long), neg (short), info (wait). 3-col
 * grid on desktop: Direction · Horizon · Entry/Stop/Target (N/A until
 * Wave 2 adds a price source — open_issues.md #34).
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
      <div
        className={`grid gap-4 ${compact ? 'grid-cols-2' : 'grid-cols-2 md:grid-cols-3'}`}
      >
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
          <Field label="Entry / Stop / Target">
            <span className="font-mono text-[13px] text-text-3">N/A (Wave 2)</span>
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
