import type { MarketRegime } from '../../api/types';
import { REGIMES } from '../../lib/constants';
import { regimeColorToken } from '../../lib/colors';
import { colorMix, cssVar } from '../../lib/colorMix';

interface RegimeIndicatorProps {
  state: MarketRegime;
  /** Classifier confidence 1–10. */
  confidence: number;
  size?: number;
}

const REGIME_ORDER = Object.keys(REGIMES) as MarketRegime[];

/**
 * Glanceable regime gauge — a 5-segment semicircle with the active Wyckoff
 * state lit, plus the glyph + label + confidence. Ported from the prototype;
 * segment colors come from `regimeColorToken` (inactive segments are a 16%
 * tint over `bg-3`).
 */
export function RegimeIndicator({ state, confidence, size = 140 }: RegimeIndicatorProps) {
  const reg = REGIMES[state];
  const activeIdx = REGIME_ORDER.indexOf(state);
  const n = REGIME_ORDER.length;
  const r0 = 46;
  const r1 = 64;
  const cx = 70;
  const cy = 70;

  return (
    <div className="flex items-center gap-[18px]">
      <div className="relative" style={{ width: size, height: size / 2 + 10 }}>
        <svg viewBox="0 0 140 80" width={size} height={size / 2 + 10} aria-label={`Market regime: ${reg.label}`}>
          {REGIME_ORDER.map((s, i) => {
            const a0 = Math.PI - (i / n) * Math.PI;
            const a1 = Math.PI - ((i + 1) / n) * Math.PI;
            const x0 = cx + r0 * Math.cos(a0);
            const y0 = cy - r0 * Math.sin(a0);
            const x1 = cx + r1 * Math.cos(a0);
            const y1 = cy - r1 * Math.sin(a0);
            const x2 = cx + r1 * Math.cos(a1);
            const y2 = cy - r1 * Math.sin(a1);
            const x3 = cx + r0 * Math.cos(a1);
            const y3 = cy - r0 * Math.sin(a1);
            const token = regimeColorToken(s);
            const active = i === activeIdx;
            return (
              <path
                key={s}
                d={`M${x0} ${y0} L${x1} ${y1} A${r1} ${r1} 0 0 1 ${x2} ${y2} L${x3} ${y3} A${r0} ${r0} 0 0 0 ${x0} ${y0} Z`}
                fill={active ? cssVar(token) : colorMix(token, 16, cssVar('--bg-3'))}
                stroke="var(--bg-2)"
                strokeWidth="1.5"
              />
            );
          })}
        </svg>
      </div>
      <div>
        <div className="flex items-center gap-2">
          <span style={{ color: cssVar(regimeColorToken(state)), fontSize: 18 }}>{reg.glyph}</span>
          <span className="text-[18px] font-semibold">{reg.label}</span>
        </div>
        <div className="font-mono text-[11px] text-t3 mt-1">confidence {confidence}/10</div>
      </div>
    </div>
  );
}
