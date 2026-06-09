import type { FactorVote } from '../../api/types';
import { colorMix, cssVar } from '../../lib/colorMix';
import { Card, SectionHeader } from '../ui';

interface ConfirmationSectionProps {
  score: number;
  votes: Record<string, FactorVote>;
}

const FACTORS: Array<{ key: string; label: string }> = [
  { key: 'price', label: 'Price' },
  { key: 'regime', label: 'Regime' },
  { key: 'news', label: 'News' },
];

function reasonFor(vote: FactorVote | undefined): string {
  if (vote && typeof vote['reason'] === 'string') return vote['reason'] as string;
  const v = vote?.vote ?? 0;
  if (v > 0) return 'Confirms the signal direction.';
  if (v < 0) return 'Counters the signal direction.';
  return 'No clear contribution.';
}

/**
 * Multi-factor confirmation — ported to the prototype's titled card + 3-col
 * factor tiles (✓ / ✗ / · vote pip + reason). Reads the real `factor_votes`
 * JSONB (vote -1/0/+1; reason when present, else a vote-derived line).
 */
export function ConfirmationSection({ score, votes }: ConfirmationSectionProps) {
  return (
    <Card>
      <SectionHeader kicker="Confirmation" title={`Multi-factor voting · ${score}/3`} />
      <div className="grid grid-cols-1 xs:grid-cols-3 gap-3">
        {FACTORS.map(({ key, label }) => {
          const v = votes[key];
          const vote = v?.vote ?? 0;
          const token = vote > 0 ? '--win' : vote < 0 ? '--loss' : null;
          const glyph = vote > 0 ? '✓' : vote < 0 ? '✗' : '·';
          return (
            <div key={key} className="p-3.5 bg-bg-1 border border-line-1 rounded-md">
              <div className="flex justify-between items-center mb-2">
                <span className="font-mono text-[11px] text-t2 capitalize tracking-[0.04em]">
                  {label}
                </span>
                <span
                  className="w-[18px] h-[18px] rounded-full inline-flex items-center justify-center text-[11px] font-semibold"
                  style={
                    token
                      ? { background: colorMix(token, 16), color: cssVar(token) }
                      : { background: 'var(--bg-3)', color: 'var(--t4)' }
                  }
                >
                  {glyph}
                </span>
              </div>
              <div className="text-[12px] text-t3 leading-[1.5]">{reasonFor(v)}</div>
            </div>
          );
        })}
      </div>
    </Card>
  );
}
