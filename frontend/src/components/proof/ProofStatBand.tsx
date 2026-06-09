import type { TrackRecordSummary } from '../../api/types';
import { formatConfidence, formatNumber, formatPctRaw } from '../../lib/format';

interface ProofStatBandProps {
  summary: TrackRecordSummary;
}

/**
 * Five-up headline proof band — ported from the prototype's `ProofStatBand`.
 * Every number is real (`TrackRecordSummary`); `hit_rate_pct` is already a
 * percent. Hairline gaps via a 1px line-colored grid background.
 */
export function ProofStatBand({ summary }: ProofStatBandProps) {
  const items: Array<{ label: string; value: string; tone?: string }> = [
    { label: 'Signals scored', value: formatNumber(summary.total_evaluated) },
    {
      label: '72h hit rate',
      value: summary.hit_rate_pct === null ? '—' : formatPctRaw(summary.hit_rate_pct, 1),
      tone: 'text-win',
    },
    { label: 'Targets hit', value: formatNumber(summary.targets_hit), tone: 'text-win' },
    { label: 'Stops hit', value: formatNumber(summary.stops_hit), tone: 'text-loss' },
    {
      label: 'Conf · hits vs misses',
      value: `${formatConfidence(summary.avg_confidence_hits)} / ${formatConfidence(summary.avg_confidence_misses)}`,
    },
  ];

  return (
    <div
      className="grid grid-cols-2 xs:grid-cols-3 md:grid-cols-5 gap-px bg-line-2 border border-line-2 rounded-lg overflow-hidden"
    >
      {items.map((m) => (
        <div key={m.label} className="bg-bg-2 p-5">
          <div className="font-mono text-[9.5px] text-t3 tracking-[0.1em] uppercase mb-2.5">
            {m.label}
          </div>
          <div
            className={`tabular-nums text-[24px] font-semibold ${m.tone ?? 'text-t1'}`}
            style={{ letterSpacing: '-0.02em' }}
          >
            {m.value}
          </div>
        </div>
      ))}
    </div>
  );
}
