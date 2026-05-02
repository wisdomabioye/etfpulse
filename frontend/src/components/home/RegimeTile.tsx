import { Link } from 'react-router-dom';
import type { MarketRegime, SignalPosture } from '../../api/types';
import { PostureBadge } from '../regime/PostureBadge';
import { RegimeBadge } from '../regime/RegimeBadge';

interface RegimeTileProps {
  regime: MarketRegime | null;
  posture: SignalPosture | null;
  /** True while the dashboard fetch is in flight or has errored. We render
   *  a "regime not yet classified" caption rather than a hollow card. */
  unavailable: boolean;
}

/**
 * Compact home-page surface for the live regime read.
 *
 * Why this is NOT `RegimeCard`: the home stats endpoint deliberately omits
 * `confidence`, `reasoning`, `macro_events_nearby`, and `classified_at` to
 * keep the dashboard payload small (#103 design — surface the headline tile
 * without forcing a second `/api/regime` roundtrip per page load). RegimeCard
 * is the rich version on `/regime`. This tile is the pointer-to-it on home.
 *
 * The tile is always rendered (non-conditional in `Home.tsx`) so the
 * page layout doesn't reflow when the data arrives — empty/error states
 * collapse to a single "not yet classified" caption.
 */
export function RegimeTile({ regime, posture, unavailable }: RegimeTileProps) {
  return (
    <div className="border border-border-2 rounded-lg bg-bg-2 overflow-hidden">
      <div className="flex items-center justify-between px-4 py-2.5 bg-bg-3 border-b border-border-2 font-mono text-[10px] uppercase tracking-[0.1em] text-text-3">
        <span>Market Regime</span>
        <Link
          to="/regime"
          className="text-accent text-[11px] font-medium hover:opacity-80"
        >
          Full breakdown →
        </Link>
      </div>
      <div className="px-5 py-4">
        {unavailable || regime === null ? (
          <div className="text-[13px] text-text-3 font-mono">
            Regime not yet classified.
          </div>
        ) : (
          <div className="flex items-center gap-2.5 flex-wrap">
            <RegimeBadge regime={regime} size="md" />
            {posture && <PostureBadge posture={posture} />}
          </div>
        )}
      </div>
    </div>
  );
}
