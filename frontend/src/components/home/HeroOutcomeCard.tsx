import { Link } from 'react-router-dom';
import type { HeroOutcome } from '../../api/types';
import { formatAgo, formatSignalType, formatUsdPrice } from '../../lib/format';

/**
 * PR E.2 — single hero outcome card.
 *
 * Two variants, same shape: "target hit" frames a captured gain, "stop
 * saved" frames an avoided drawdown. Both link through to `/signals/:id`
 * so visitors can verify the call.
 *
 * Layout (per HomeV3 mock pattern):
 *   ┌─────────────────────────────────────────────┐
 *   │ TARGET HIT · BTC LONG          2d ago       │  <- header strip
 *   ├─────────────────────────────────────────────┤
 *   │                                             │
 *   │  +2.4% to target                            │  <- magnitude line, 28px
 *   │  BTC ETF outflow snapped 4-day streak       │  <- headline, 14px
 *   │                                             │
 *   │  Entry $84,200 → Target $86,235             │  <- level line, mono 12px
 *   │                                             │
 *   │  View signal →                              │  <- footer CTA
 *   └─────────────────────────────────────────────┘
 *
 * Mirrors the visual rhythm of `HeroHitRatePanel` (bg-2 body, bg-3 header
 * strip, monospace 10px header label, tabular-nums for the magnitude). The
 * outcome card and hit-rate panel are visually siblings on the home page.
 *
 * `max_favorable` / `max_adverse` arrive as fractions over the wire; we
 * compute the display number from `entry_price` + `target_price` (target
 * hit) or `entry_price` + `max_adverse` (stop saved) so the framing is
 * tied to ACTUAL trade outcomes, not the observed peak/trough during the
 * window. Same rationale as the backend response shape — pre-computing on
 * the FE keeps the API surface minimal.
 *
 * Direction (long/short) is included in the header alongside the asset
 * because the trade thesis is part of the story — "bot called BTC LONG,
 * hit target" is information; "BTC, hit target" leaves the visitor
 * guessing which way the call leaned.
 *
 * Age framing: `formatAgo(signal_created_at)` is unbounded — a card
 * surfacing an 11-month-old outcome will say "11mo ago". This is a
 * deliberate honesty choice (no curation), NOT an oversight. If the bot
 * stops producing wins, the card date going stale is the truthful signal.
 * Don't silently filter old outcomes here.
 */

interface HeroOutcomeCardProps {
  outcome: HeroOutcome;
  variant: 'target_hit' | 'stop_saved';
}

export function HeroOutcomeCard({ outcome, variant }: HeroOutcomeCardProps) {
  const entry = Number(outcome.entry_price);
  const isTargetHit = variant === 'target_hit';

  // Magnitude line — % move that defines the card.
  //
  // For target_hit: actual gain from entry to target (entry → target, sign
  // implied by `direction`). This is the DETERMINISTIC trade outcome at the
  // moment hit_target fired; `max_favorable` (which could be larger if price
  // overshot target) isn't the right number for "+X% to target" framing.
  //
  // For stop_saved: `max_adverse` expressed as percent. Backend already
  // guards `max_adverse > 0` so we know there's a real drawdown to show.
  let magnitudePct: number | null = null;
  if (isTargetHit && outcome.target_price !== null && entry > 0) {
    const target = Number(outcome.target_price);
    magnitudePct = (Math.abs(target - entry) / entry) * 100;
  } else if (!isTargetHit && outcome.max_adverse !== null) {
    magnitudePct = Number(outcome.max_adverse) * 100;
  }
  const magnitudeText =
    magnitudePct !== null && Number.isFinite(magnitudePct)
      ? `${magnitudePct.toFixed(1)}%`
      : '—';

  const headerLabel = isTargetHit ? 'TARGET HIT' : 'STOP SAVED';
  // Both variants use `text-pos` (green). The "stop saved X% drawdown"
  // framing is POSITIVE — money preserved by stop discipline. Using
  // `text-warn` (amber) here would suggest "risk/caution" which is the
  // opposite read of what happened (the bot's stop avoided a worse loss).
  // Header labels carry the differentiation, not color.
  const magnitudeColor = 'text-pos';
  // Lead-in word in front of the magnitude — "+" prefix for the gain
  // framing, "Saved" prefix for the drawdown-avoided framing. Both stay
  // honest about what happened.
  const magnitudePrefix = isTargetHit ? '+' : 'Saved ';
  const magnitudeSuffix = isTargetHit ? ' to target' : ' drawdown';
  // Screen-reader narration — fires before the visual content. Variant +
  // asset + direction + magnitude in one sentence so a screen-reader user
  // gets the headline before tabbing into details. `magnitudeText` already
  // carries the "%" suffix.
  const ariaLabel = `${headerLabel.toLowerCase()}: ${outcome.asset} ${outcome.direction} signal, ${magnitudePrefix.trim()}${magnitudeText}${magnitudeSuffix}. View signal detail.`;

  return (
    <Link
      to={`/signals/${outcome.signal_id}`}
      aria-label={ariaLabel}
      className="block border border-border-2 rounded-[10px] bg-bg-2 overflow-hidden hover:border-border-1 transition-colors"
    >
      {/* Header strip — variant label + asset + direction + relative time. */}
      <div className="flex items-center justify-between px-4 py-2.5 bg-bg-3 border-b border-border-2 font-mono text-[10px] uppercase tracking-[0.1em] text-text-3">
        <span>
          {headerLabel} <span className="text-text-4">·</span>{' '}
          <span className="text-text-2">{outcome.asset}</span>{' '}
          <span className="text-text-3">{outcome.direction}</span>
        </span>
        <span className="text-text-4">{formatAgo(outcome.signal_created_at)}</span>
      </div>

      {/* Body */}
      <div className="px-5 pt-5 pb-4">
        <div
          className={`${magnitudeColor} font-semibold tabular-nums leading-none`}
          style={{ fontSize: 28, letterSpacing: '-0.02em' }}
        >
          {magnitudePrefix}
          {magnitudeText}
          <span className="text-text-3" style={{ fontSize: 14, fontWeight: 500 }}>
            {magnitudeSuffix}
          </span>
        </div>

        {/* Headline (1 line, truncated). When the AI didn't emit a headline
            we fall back to the signal-type label so the card never looks
            broken. Defensive — practically unreachable since hit_target /
            hit_stop require AI success. */}
        <div className="mt-3 text-[14px] leading-[1.4] text-text-2 line-clamp-1">
          {outcome.headline ?? formatSignalType(outcome.signal_type)}
        </div>

        {/* Level line — what the trade actually looked like. */}
        <div className="mt-3 font-mono text-[12px] text-text-3">
          {renderLevels(outcome, isTargetHit)}
        </div>

        {/* CTA */}
        <div className="mt-4 text-accent text-[13px] font-medium">
          View signal →
        </div>
      </div>
    </Link>
  );
}

/** Level line varies by variant: target_hit shows entry → target, stop_saved
 *  shows entry + stop. Pulled out of the JSX so the conditional doesn't
 *  clutter the layout structure. */
function renderLevels(outcome: HeroOutcome, isTargetHit: boolean): string {
  const entry = formatUsdPrice(Number(outcome.entry_price));
  if (isTargetHit && outcome.target_price !== null) {
    return `Entry ${entry} → Target ${formatUsdPrice(Number(outcome.target_price))}`;
  }
  if (!isTargetHit && outcome.stop_price !== null) {
    return `Entry ${entry}, Stop ${formatUsdPrice(Number(outcome.stop_price))}`;
  }
  // Either variant with a missing complementary level — render just entry.
  // Backend filters out outcomes without the relevant level (hero query
  // requires target_price for target_hit, stop_price for stop_saved), so
  // this branch is unreachable in practice but renders cleanly if hit.
  return `Entry ${entry}`;
}
