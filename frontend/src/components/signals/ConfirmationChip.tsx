interface ConfirmationChipProps {
  score: number | null;
  size?: 'sm' | 'lg';
}

/**
 * PR I.2 — 0..3 cross-factor confirmation readout.
 *
 * Sums per-factor votes (price, regime, news) into a single integer. Higher =
 * more independent factors agreed with the AI's direction. Renders nothing
 * when `score` is null (scoring didn't apply: wait action, AI-failed, MARKET
 * sentinel, or pre-I.2 historical row). The breakdown lives on `/signals/:id`.
 *
 * Color thresholds mirror ConfidenceBadge's positive/warn/negative pattern:
 *   3 → pos · 2 → pos · 1 → warn · 0 → neg
 *
 * The "/3" suffix mirrors ConfidenceBadge's "/10" — visually parallel so users
 * read both as "value out of max" without a tooltip.
 */
export function ConfirmationChip({ score, size = 'sm' }: ConfirmationChipProps) {
  if (score === null) return null;

  const color =
    score >= 2 ? 'var(--color-pos)' : score === 1 ? 'var(--color-warn)' : 'var(--color-neg)';
  const bg = `color-mix(in oklab, ${color} 14%, transparent)`;
  const borderColor = `color-mix(in oklab, ${color} 30%, transparent)`;

  if (size === 'lg') {
    return (
      <div
        className="inline-flex flex-col items-start rounded-lg px-[14px] py-[10px]"
        style={{ background: bg, border: `1px solid ${borderColor}` }}
      >
        <div className="flex items-center gap-2">
          <span
            className="w-2 h-2 rounded-full"
            style={{ background: color, boxShadow: `0 0 8px ${color}` }}
          />
          <span className="font-mono text-[22px] font-semibold text-text-1 tabular-nums">
            {score}
            <span className="text-text-3">/3</span>
          </span>
        </div>
        <div className="font-mono text-[10px] text-text-3 uppercase tracking-[0.1em] mt-0.5">
          confirmation
        </div>
      </div>
    );
  }

  return (
    <span
      className="inline-flex items-center gap-1.5 rounded-[5px] px-2 py-[3px] font-mono text-[11px] font-semibold text-text-1 tabular-nums"
      style={{ background: bg, border: `1px solid ${borderColor}` }}
      title={`Confirmation score ${score}/3 — sum of price/regime/news factor votes`}
    >
      <span className="w-1.5 h-1.5 rounded-full" style={{ background: color }} />
      {score}
      <span className="text-text-3 font-normal">/3</span>
    </span>
  );
}
