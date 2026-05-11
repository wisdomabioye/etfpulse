import { Link } from 'react-router-dom';
import type { MarketRegime, SignalPosture } from '../../api/types';
import { PostureBadge } from '../regime/PostureBadge';
import { RegimeBadge } from '../regime/RegimeBadge';

/** Plain-English caption for each Wyckoff-style phase. The badges alone
 *  ("uncertain", "cautious") are jargon to anyone outside trading — the
 *  caption is the difference between a confused first-time visitor and one
 *  who reads the tile and understands what we're saying. Kept short
 *  (≤ ~120 chars) so the tile stays compact; the longer reasoning lives
 *  on `/regime`. */
const REGIME_CAPTIONS: Record<MarketRegime, string> = {
  accumulation:
    'Smart money quietly building positions while price is still flat or basing — early bull setup.',
  markup:
    'Sustained upward trend with broad participation — bull phase in full swing.',
  distribution:
    'Smart money quietly offloading into strength while price holds elevated — late-cycle setup.',
  markdown:
    'Sustained downward trend — bear phase in full swing.',
  uncertain:
    'No clear directional bias from flows, news, or macro context — wait for confirmation.',
};

/** What the posture means for our alerting cadence. Operator-facing copy
 *  ("we'll fire fewer alerts") rather than market commentary. */
const POSTURE_CAPTIONS: Record<SignalPosture, string> = {
  aggressive: 'Firing alerts on weaker confirmation — high-conviction regime.',
  normal: 'Standard alerting — alerts when detectors agree.',
  cautious: 'Higher bar to alert — only strongest signals get through.',
  paused: 'Alerts suspended while the regime resolves.',
};

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
          <>
            <div className="flex items-center gap-2.5 flex-wrap">
              <RegimeBadge regime={regime} size="md" />
              {posture && <PostureBadge posture={posture} />}
            </div>
            <p className="mt-2.5 text-[13px] leading-[1.55] text-text-2">
              {REGIME_CAPTIONS[regime]}
              {posture && (
                <>
                  {' '}
                  <span className="text-text-3">{POSTURE_CAPTIONS[posture]}</span>
                </>
              )}
            </p>
          </>
        )}
      </div>
    </div>
  );
}
