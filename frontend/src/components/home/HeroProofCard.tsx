import { useNavigate } from 'react-router-dom';

import type { HeroOutcome } from '../../api/types';
import { colorMix, cssVar } from '../../lib/colorMix';
import { formatPrice, formatSignedPct } from '../../lib/format';
import { AssetBadge, ConfidenceBadge, DetectorBadge } from '../ui';

interface HeroProofCardProps {
  outcome: HeroOutcome;
  kind: 'target' | 'stop';
}

/**
 * Proof-forward outcome card for the Home hero — reskinned (R4) to the
 * prototype's `HeroOutcomeCard`. Data math follows the existing (verified)
 * outcome logic, NOT the prototype's mock fields: excursions arrive as
 * fractions (×100 for percent); the headline "result" is the % move to target
 * (target hit) or the drawdown the stop bounded (stop saved). Clicking opens
 * the full signal.
 */
export function HeroProofCard({ outcome, kind }: HeroProofCardProps) {
  const navigate = useNavigate();
  const isTarget = kind === 'target';
  const token = isTarget ? '--win' : '--loss';
  const color = cssVar(token);

  const entry = Number(outcome.entry_price);
  const target = outcome.target_price !== null ? Number(outcome.target_price) : null;
  const adverse = outcome.max_adverse !== null ? Number(outcome.max_adverse) : null;
  const favorable = outcome.max_favorable !== null ? Number(outcome.max_favorable) : null;

  // Headline result: target → % move entry→target; stop → drawdown bounded.
  const result =
    isTarget && target !== null
      ? formatSignedPct(((target - entry) / entry) * 100, 1)
      : adverse !== null
        ? formatSignedPct(-adverse * 100, 1)
        : '—';

  const levels: Array<[string, number | null, string]> = [
    ['Entry', entry, cssVar('--t1')],
    ['Stop', outcome.stop_price !== null ? Number(outcome.stop_price) : null, cssVar('--loss')],
    ['Target', target, cssVar('--win')],
  ];

  return (
    <div
      role="button"
      tabIndex={0}
      onClick={() => navigate(`/signals/${outcome.signal_id}`)}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          navigate(`/signals/${outcome.signal_id}`);
        }
      }}
      className="cursor-pointer bg-bg-2 rounded-lg overflow-hidden transition-transform duration-[var(--dur-1)] hover:-translate-y-0.5"
      style={{ border: `1px solid ${colorMix(token, 26, cssVar('--line-2'))}` }}
    >
      <div
        className="flex items-center justify-between px-[15px] py-[9px]"
        style={{
          background: colorMix(token, 9),
          borderBottom: `1px solid ${colorMix(token, 18)}`,
        }}
      >
        <span
          className="font-mono text-[10px] tracking-[0.12em] uppercase font-semibold"
          style={{ color }}
        >
          {isTarget ? '◎ Last target hit' : '◇ Last stop saved'}
        </span>
        <span className="font-mono tabular-nums text-[14px] font-bold" style={{ color }}>
          {result}
        </span>
      </div>
      <div className="p-[15px]">
        <div className="flex gap-[7px] mb-2.5">
          <AssetBadge asset={outcome.asset} size="sm" />
          <DetectorBadge type={outcome.signal_type} size="sm" />
          <ConfidenceBadge value={outcome.confidence} />
        </div>
        <div className="text-[13.5px] font-medium leading-[1.45] mb-3.5 text-pretty">
          {outcome.headline ?? 'AI analysis pending'}
        </div>
        <div className="grid grid-cols-3 gap-2">
          {levels.map(([label, value, c]) => (
            <div key={label} className="px-[9px] py-[7px] bg-bg-1 rounded-sm border border-line-1">
              <div className="font-mono text-[9px] text-t4 uppercase tracking-[0.08em]">{label}</div>
              <div className="font-mono tabular-nums text-[12px] font-semibold mt-0.5" style={{ color: c }}>
                {formatPrice(value)}
              </div>
            </div>
          ))}
        </div>
        {(favorable !== null || adverse !== null) && (
          <div className="flex gap-3.5 mt-3">
            {favorable !== null && (
              <span className="font-mono tabular-nums text-[10.5px] text-t3">
                max fav <span className="text-win">{formatSignedPct(favorable * 100, 1)}</span>
              </span>
            )}
            {adverse !== null && (
              <span className="font-mono tabular-nums text-[10.5px] text-t3">
                max adv <span className="text-loss">{formatSignedPct(-adverse * 100, 1)}</span>
              </span>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
