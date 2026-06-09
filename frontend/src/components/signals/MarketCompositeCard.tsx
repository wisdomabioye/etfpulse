import type { SignalOutcome } from '../../api/types';
import type { Verdict } from './outcomeCardHelpers';
import { Section, Row } from './outcomeCardParts';

// ---------------------------------------------------------------------------
// MARKET (regime_shift) composite outcome — PR I.3b
// ---------------------------------------------------------------------------
//
// MARKET signals are scored as a weighted BTC+ETH composite return (delta)
// rather than against a single-asset entry/stop/target. All single-asset
// baseline fields are NULL by design (PR I.3b migration relaxed
// `price_at_signal` to nullable). Render the composite story directly:
// a verdict band + the signed return + the max_favorable/max_adverse
// excursions (which the evaluator still computes on the same composite
// series). No "Levels" or "+24h/+72h" rows — they don't apply.
export function MarketCompositeCard({ outcome }: { outcome: SignalOutcome }) {
  // composite_return_pct is a signed fraction (0.024 = +2.4%). Format with
  // sign and one decimal, same precision as the bot's recent-outcome line.
  const pct = (outcome.composite_return_pct ?? 0) * 100;
  const sign = pct >= 0 ? '+' : '';
  const compositeText = `${sign}${pct.toFixed(2)}%`;
  const toneClass = pct >= 0 ? 'text-pos' : 'text-neg';
  // Composite-specific verdict — mirrors the bot's `_outcome_icon_and_verdict`
  // MARKET branch. "composite hit" / "composite miss" wording rather than
  // the single-asset "Target hit" / "Neither hit" framing (no target ever
  // existed on a MARKET row).
  const verdict: Verdict =
    outcome.hit_target === true
      ? { label: '✓ Composite hit', color: 'var(--color-pos)' }
      : { label: '— Composite miss', color: 'var(--color-text-3)' };
  return (
    <div
      className="rounded-lg bg-bg-2"
      style={{
        border: '1px solid var(--color-border-2)',
        borderLeft: `3px solid ${verdict.color}`,
      }}
    >
      <div className="flex items-center justify-between px-5 py-3 border-b border-border-2 font-mono text-[10px] uppercase tracking-[0.1em] text-text-3">
        <span className="inline-flex items-center gap-2">
          Outcome
          <span
            className="font-mono text-[9px] tracking-normal normal-case text-text-3 px-1.5 py-0.5 rounded"
            style={{ border: '1px solid var(--color-border-3)' }}
            title="Scored as a weighted BTC+ETH composite return (not against a single-asset target)"
          >
            market composite
          </span>
        </span>
        <span style={{ color: verdict.color }}>{verdict.label}</span>
      </div>
      <div className="px-5 py-4 grid gap-5 grid-cols-1 md:grid-cols-2">
        <Section title="Composite return">
          <Row label="BTC+ETH" value={compositeText} secondary={null} toneClass={toneClass} />
        </Section>
        <Section title="Excursion">
          <Row
            label="Max favorable"
            value={outcome.max_favorable === null ? '—' : `${(outcome.max_favorable * 100).toFixed(1)}%`}
            secondary={null}
          />
          <Row
            label="Max adverse"
            value={outcome.max_adverse === null ? '—' : `${(outcome.max_adverse * 100).toFixed(1)}%`}
            secondary={null}
          />
        </Section>
      </div>
    </div>
  );
}
