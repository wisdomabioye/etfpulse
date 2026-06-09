import type { MarketRegime } from '../../api/types';
import { useRegimeHistory } from '../../api/queries';
import { REGIMES } from '../../lib/constants';
import { regimeColorToken } from '../../lib/colors';
import { colorMix, cssVar } from '../../lib/colorMix';
import { Card, SectionHeader, Skeleton } from '../ui';

/**
 * Regime history strip — ported from the prototype's "Last 8 days" card.
 * One glyph cell per day (tinted by the day's regime) + a legend. Reads the
 * real `/api/regime/history` (newest-first), rendered left→right oldest→newest.
 */
export function RegimeHistoryStrip() {
  const { data, isLoading } = useRegimeHistory(8);
  // API is newest-first; show chronological left→right.
  const days = data ? [...data.history].reverse() : [];

  return (
    <Card>
      <SectionHeader kicker="Last 8 days" title="Regime history" />
      {isLoading ? (
        <Skeleton className="h-[60px] w-full" />
      ) : days.length === 0 ? (
        <p className="text-t3 text-[13px]">No history recorded yet.</p>
      ) : (
        <div className="flex gap-1.5 items-stretch">
          {days.map((d) => {
            const reg = d.regime ? REGIMES[d.regime] : null;
            const token = d.regime ? regimeColorToken(d.regime) : null;
            return (
              <div key={d.date} className="flex-1 text-center">
                <div
                  className="h-[46px] rounded-sm flex items-center justify-center text-[15px] mb-[7px]"
                  style={
                    token
                      ? {
                          background: colorMix(token, 22, cssVar('--bg-3')),
                          border: `1px solid ${colorMix(token, 35)}`,
                          color: cssVar(token),
                        }
                      : { background: 'var(--bg-3)', border: '1px solid var(--line-2)', color: 'var(--t4)' }
                  }
                  title={`${d.date}${reg ? ` · ${reg.label}` : ''}`}
                >
                  {reg ? reg.glyph : '·'}
                </div>
                <div className="font-mono text-[9.5px] text-t4">{d.date.slice(8, 10)}</div>
              </div>
            );
          })}
        </div>
      )}

      <div className="flex gap-4 mt-[18px] flex-wrap">
        {(Object.keys(REGIMES) as MarketRegime[]).map((key) => (
          <span key={key} className="inline-flex items-center gap-1.5 text-[11px] text-t3">
            <span style={{ color: cssVar(regimeColorToken(key)) }}>{REGIMES[key].glyph}</span>
            {REGIMES[key].label}
          </span>
        ))}
      </div>
    </Card>
  );
}
