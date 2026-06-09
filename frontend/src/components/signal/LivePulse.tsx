import type { SignalListItem } from '../../api/types';
import { formatAgo, isWithin } from '../../lib/format';
import { AssetBadge, DetectorIcon } from '../ui';

interface LivePulseProps {
  /** Recent signals, newest first (caller supplies real data — no simulation). */
  signals: SignalListItem[];
  limit?: number;
  onPick?: (signal: SignalListItem) => void;
}

const FRESH_MS = 2 * 60 * 1000; // < 2min → "just arrived" highlight + entrance

/**
 * Live pulse rail — a compact stream of the most recent signals. Ported from
 * the prototype's `LivePulse`, but the prototype's faked arrivals (a
 * `setInterval` that spliced in `Math.random()` signals) are removed: this
 * renders the REAL recent signals passed in and highlights genuinely-fresh
 * ones (< 2min old) with the amber-soft entrance (`pulseIn`, motion-gated).
 */
export function LivePulse({ signals, limit = 6, onPick }: LivePulseProps) {
  const items = signals.slice(0, limit);
  return (
    <div className="flex flex-col gap-2">
      {items.map((s) => {
        const fresh = isWithin(s.created_at, FRESH_MS);
        return (
          <div
            key={s.id}
            role="button"
            tabIndex={0}
            onClick={() => onPick?.(s)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault();
                onPick?.(s);
              }
            }}
            className={`grid items-center gap-2.5 px-3 py-[9px] rounded-sm cursor-pointer border ${
              fresh ? 'bg-acc-soft border-acc-line' : 'bg-bg-2 border-line-1'
            }`}
            style={{
              gridTemplateColumns: 'auto auto 1fr auto',
              animation: fresh ? 'pulseIn 0.4s var(--ease)' : undefined,
            }}
          >
            <AssetBadge asset={s.asset} size="sm" />
            <DetectorIcon type={s.signal_type} size={12} />
            <span className="text-[11.5px] text-t2 truncate">
              {s.headline ?? 'AI analysis pending'}
            </span>
            <span className={`font-mono text-[10px] whitespace-nowrap ${fresh ? 'text-acc-hi' : 'text-t4'}`}>
              {formatAgo(s.created_at)}
            </span>
          </div>
        );
      })}
    </div>
  );
}
