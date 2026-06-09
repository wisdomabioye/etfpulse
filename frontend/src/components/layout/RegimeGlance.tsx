import { Link } from 'react-router-dom';

import { useDashboardStats } from '../../api/queries';
import { REGIMES } from '../../lib/constants';
import { regimeColorToken } from '../../lib/colors';
import { cssVar } from '../../lib/colorMix';
import { narrowPosture, narrowRegime } from '../../lib/format';

/**
 * Compact regime + posture readout for the TopNav's status row. Mirrors the
 * prototype's row-1 right cluster: a clickable "Regime" glance (glyph + label,
 * colored) linking to /regime, plus the current signal posture in amber.
 * Hides each half independently when its data hasn't loaded / is out-of-enum
 * (cold boot) rather than rendering a placeholder in the nav.
 */
export function RegimeGlance() {
  const stats = useDashboardStats();
  const regime = narrowRegime(stats.data?.current_regime);
  const posture = narrowPosture(stats.data?.signal_posture);

  if (!regime && !posture) return null;

  return (
    <div className="flex items-center gap-4">
      {regime && (
        <Link to="/regime" className="flex items-center gap-[7px]">
          <span className="font-mono text-[10px] text-t4 tracking-[0.1em] uppercase">Regime</span>
          <span style={{ color: cssVar(regimeColorToken(regime)), fontSize: 12 }}>
            {REGIMES[regime].glyph}
          </span>
          <span className="font-mono text-[11px]" style={{ color: cssVar(regimeColorToken(regime)) }}>
            {REGIMES[regime].label}
          </span>
        </Link>
      )}
      {regime && posture && <span className="w-px h-3 bg-line-2" aria-hidden />}
      {posture && (
        <span className="font-mono text-[10px] text-t4">
          posture: <span className="text-warn capitalize">{posture}</span>
        </span>
      )}
    </div>
  );
}
