import type { RegimeResponse } from '../../api/types';
import { formatAgo } from '../../lib/format';
import { PostureBadge } from './PostureBadge';
import { RegimeBadge } from './RegimeBadge';

interface RegimeCardProps {
  regime: RegimeResponse;
}

/**
 * Hero block for the `/regime` page — surfaces the current regime, posture,
 * confidence, and any nearby macro events. Pure presentation; the page owns
 * the `useRegime` fetch.
 *
 * NOT used on the home page: the home tile (`components/home/RegimeTile`)
 * is a smaller surface that reads from `DashboardStats` (which deliberately
 * omits `confidence`/`reasoning`/`macro_events_nearby` to keep the home
 * payload small per #103). If a future caller wants this rich view inline
 * elsewhere, expose a "View regime →" link here behind a prop.
 */
export function RegimeCard({ regime }: RegimeCardProps) {
  const events = regime.macro_events_nearby;

  return (
    <div className="border border-border-2 rounded-lg bg-bg-2 overflow-hidden">
      {/* Header strip — matches HeroHitRatePanel chrome for visual parity. */}
      <div className="flex items-center justify-between px-4 py-2.5 bg-bg-3 border-b border-border-2 font-mono text-[10px] uppercase tracking-[0.1em] text-text-3">
        <span>Market Regime</span>
        <span title={new Date(regime.classified_at).toISOString()}>
          {formatAgo(regime.classified_at)}
        </span>
      </div>

      <div className="px-5 py-5">
        <div className="flex items-center gap-2.5 flex-wrap mb-4">
          <RegimeBadge regime={regime.regime} size="md" />
          <PostureBadge posture={regime.signal_posture} />
          <span
            className="inline-flex items-center gap-1.5 rounded-full border border-border-2 bg-bg-3 font-mono uppercase tracking-[0.08em] text-[11px] px-2 py-0.5 text-text-2"
            title="Classifier confidence (1 = barely above noise, 10 = strong evidence)"
          >
            <span className="text-text-3">conf</span>
            <span className="tabular-nums text-text-1">{regime.confidence}/10</span>
          </span>
        </div>

        {events.length > 0 && (
          <div className="border-t border-border-2 pt-3">
            <div className="font-mono text-[10px] text-text-3 uppercase tracking-[0.1em] mb-2">
              Macro events nearby
            </div>
            <ul className="m-0 p-0 list-none flex flex-col gap-1.5">
              {/* Index-suffixed key — two events with the same human-readable
                  label (e.g. two FOMC meetings within the ±N-day window) are
                  legitimate input and would collide on key={label} alone. */}
              {events.map((label, i) => (
                <li
                  key={`${label}-${i}`}
                  className="text-[13px] text-text-1 font-mono"
                  style={{ textWrap: 'pretty' }}
                >
                  · {label}
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </div>
  );
}
