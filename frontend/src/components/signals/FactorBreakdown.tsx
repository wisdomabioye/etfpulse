import type { FactorVote } from '../../api/types';

interface FactorBreakdownProps {
  score: number | null;
  votes: Record<string, FactorVote> | null;
}

/**
 * PR I.2 — per-factor vote breakdown card for /signals/:id.
 *
 * Three rows in fixed order (price, regime, news) so signals are visually
 * comparable across the detail page. Each row shows the factor's vote sign
 * + the `reason` string the backend wrote into the JSONB. Renders nothing
 * when scoring didn't apply (null score AND null votes — same gate as the
 * chip).
 */
export function FactorBreakdown({ score, votes }: FactorBreakdownProps) {
  if (score === null || votes === null) return null;

  const rows: Array<{ key: string; label: string }> = [
    { key: 'price', label: 'Price' },
    { key: 'regime', label: 'Regime' },
    { key: 'news', label: 'News' },
  ];

  return (
    <div className="bg-bg-2 border border-border-2 rounded-lg p-5">
      <div className="flex items-center justify-between mb-4">
        <h3 className="font-mono text-[11px] text-text-3 uppercase tracking-[0.1em]">
          Multi-factor confirmation
        </h3>
        <span className="font-mono text-[11px] text-text-3 tabular-nums">
          {score}/3
        </span>
      </div>
      <div className="space-y-2">
        {rows.map(({ key, label }) => {
          const v = votes[key];
          if (!v) return null;
          return <FactorRow key={key} label={label} vote={v} />;
        })}
      </div>
    </div>
  );
}

function FactorRow({ label, vote }: { label: string; vote: FactorVote }) {
  const color =
    vote.vote === 1
      ? 'var(--color-pos)'
      : vote.vote === -1
        ? 'var(--color-neg)'
        : 'var(--color-text-4)';
  const sign = vote.vote === 1 ? '+1' : vote.vote === -1 ? '−1' : '0';
  const reason = typeof vote['reason'] === 'string' ? (vote['reason'] as string) : '';
  return (
    <div className="flex items-center justify-between gap-3 py-1.5">
      <div className="flex items-center gap-2 min-w-0">
        <span
          className="w-1.5 h-1.5 rounded-full shrink-0"
          style={{ background: color }}
        />
        <span className="text-[13px] text-text-1">{label}</span>
        <span className="text-[12px] text-text-3 truncate">{reason}</span>
      </div>
      <span
        className="font-mono text-[12px] tabular-nums shrink-0"
        style={{ color }}
      >
        {sign}
      </span>
    </div>
  );
}
