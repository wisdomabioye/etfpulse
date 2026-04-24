import { Link } from 'react-router-dom';
import type { SignalListItem } from '../../api/types';
import { formatAgo } from '../../lib/format';
import { AssetBadge } from './AssetBadge';
import { ConfidenceBadge } from './ConfidenceBadge';
import { SignalTypeBadge } from './SignalTypeBadge';

interface SignalCardProps {
  signal: SignalListItem;
  /** Comfortable (default) or compact — mock density token. */
  compact?: boolean;
}

/**
 * Card A variant — per user's locked-in design choice.
 *
 * Anatomy (top → bottom):
 *   1. Badges row: [AssetBadge + SignalTypeBadge on left] [ConfidenceBadge on right]
 *   2. Headline: 15px semibold (or 14px in compact)
 *   3. Meta row: mono 11px, "#42 · 2h ago · swing", with border-top divider
 *
 * Whole card is a <Link> → /signals/:id. Hover lifts 1px + border tints
 * accent. "active" state (focus-visible) adds a 3px accent left-bar for
 * keyboard users.
 *
 * NULL ai_analysis handling: hides the ConfidenceBadge entirely; headline
 * falls back to "AI analysis unavailable" in muted text.
 */
export function SignalCard({ signal, compact = false }: SignalCardProps) {
  const headlineText = signal.headline ?? 'AI analysis unavailable';
  const headlineMuted = signal.headline === null;

  return (
    <Link
      to={`/signals/${signal.id}`}
      className={`block bg-bg-2 border border-border-2 rounded-lg transition-colors duration-150 hover:border-border-3 hover:bg-bg-3 focus-visible:outline-none focus-visible:border-accent ${
        compact ? 'px-4 py-[14px]' : 'px-5 py-[18px]'
      }`}
    >
      {/* Badges row */}
      <div
        className={`flex items-center justify-between ${compact ? 'mb-3' : 'mb-4'}`}
      >
        <div className="flex items-center gap-2">
          <AssetBadge asset={signal.asset} />
          <SignalTypeBadge type={signal.signal_type} />
        </div>
        <ConfidenceBadge value={signal.confidence} />
      </div>

      {/* Headline */}
      <div
        className={`${compact ? 'text-[14px]' : 'text-[15px]'} leading-[1.45] ${headlineMuted ? 'text-text-3 italic' : 'text-text-1 font-semibold'}`}
        style={{ letterSpacing: '-0.01em', textWrap: 'pretty' }}
      >
        {headlineText}
      </div>

      {/* Meta — #id · ago · horizon */}
      <div
        className={`flex items-center gap-2.5 font-mono text-[11px] text-text-3 ${
          compact ? 'pt-3 mt-3' : 'pt-3.5 mt-4'
        } border-t border-border-1`}
      >
        <span>#{signal.id}</span>
        <span className="text-text-4">·</span>
        <span>{formatAgo(signal.created_at)}</span>
        {signal.time_horizon && (
          <>
            <span className="text-text-4">·</span>
            <span>{signal.time_horizon}</span>
          </>
        )}
      </div>
    </Link>
  );
}
