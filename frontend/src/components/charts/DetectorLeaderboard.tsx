import { DETECTORS } from '../../lib/constants';
import { ciToneToken } from '../../lib/colors';
import { colorMix, cssVar } from '../../lib/colorMix';
import { formatPct } from '../../lib/format';
import { DetectorIcon } from '../ui';
import type { LeaderboardRow } from './types';

interface DetectorLeaderboardProps {
  data: LeaderboardRow[];
}

/**
 * Per-detector precision leaderboard — empirical hit rate + a Wilson-CI bar
 * per detector, toned by whether the CI clears 0.5 (`ciToneToken`). Ported
 * from the prototype; the prototype's `transl(...)` typo (a no-op transform
 * that left the center tick un-centered) is fixed to `translate(...)`.
 */
export function DetectorLeaderboard({ data }: DetectorLeaderboardProps) {
  return (
    <div className="flex flex-col">
      {data.map((d, idx) => {
        const det = DETECTORS[d.key];
        const toneToken = ciToneToken(d.ci_low, d.ci_high);
        const tone = cssVar(toneToken);
        return (
          <div
            key={d.key}
            className="grid items-center gap-3.5 py-[13px]"
            style={{
              gridTemplateColumns: '24px 150px 1fr 86px',
              borderTop: idx ? '1px solid var(--line-1)' : 'none',
            }}
          >
            <span className="font-mono tabular-nums text-[12px] text-t4">
              {String(idx + 1).padStart(2, '0')}
            </span>
            <div className="flex items-center gap-2">
              <DetectorIcon type={d.key} size={14} />
              <span className="text-[13px] font-medium">{det.label}</span>
            </div>
            {/* CI bar with center tick + point */}
            <div
              className="relative h-[18px]"
              title={`${formatPct(d.hit)} · 95% CI ${formatPct(d.ci_low, 0)}–${formatPct(d.ci_high, 0)} · n=${d.n}`}
            >
              <div className="absolute left-0 right-0 h-px bg-line-2" style={{ top: '50%' }} />
              <div
                className="absolute w-px h-[11px] bg-line-3"
                style={{ top: '50%', left: '50%', transform: 'translate(-50%,-50%)' }}
              />
              {/* CI whisker */}
              <div
                className="absolute h-[5px] rounded-full"
                style={{
                  top: '50%',
                  transform: 'translateY(-50%)',
                  background: colorMix(toneToken, 30),
                  left: `${d.ci_low * 100}%`,
                  width: `${(d.ci_high - d.ci_low) * 100}%`,
                }}
              />
              {/* point */}
              <div
                className="absolute w-2.5 h-2.5 rounded-full"
                style={{
                  top: '50%',
                  left: `${d.hit * 100}%`,
                  transform: 'translate(-50%,-50%)',
                  background: tone,
                  border: '2px solid var(--bg-2)',
                }}
              />
            </div>
            <div className="text-right">
              <div className="font-mono tabular-nums text-[14px] font-semibold" style={{ color: tone }}>
                {formatPct(d.hit)}
              </div>
              <div className="font-mono tabular-nums text-[9.5px] text-t4">
                n={d.n} · CI {formatPct(d.ci_low, 0)}–{formatPct(d.ci_high, 0)}
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
}
