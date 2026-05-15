import type { HeroOutcome } from '../../api/types';
import { Skeleton } from '../ui';
import { HeroOutcomeCard } from './HeroOutcomeCard';

/**
 * PR E.2 — container for the hero outcome cards.
 *
 * Renders zero, one, or two `HeroOutcomeCard`s based on which backend
 * fields are populated. Returns `null` when both are null so the home
 * page's wrapping `<section>` can collapse entirely — no hollow
 * "nothing to show yet" placeholder taking vertical space on a cold-start
 * DB.
 *
 * The home page wraps this in a `<section>` that ALSO conditionally
 * renders. We do the null-check here too so a caller that forgets the
 * outer guard doesn't render an empty container.
 */

interface HeroOutcomeRowProps {
  targetHit: HeroOutcome | null;
  stopSaved: HeroOutcome | null;
  /** When true, render placeholder skeletons so the home page doesn't
   *  collapse-then-expand as stats resolve. */
  loading?: boolean;
}

// Height tuned to match the rendered HeroOutcomeCard (header strip + body
// with magnitude/headline/levels/CTA) so the skeleton-to-real swap doesn't
// jolt the page.
const HERO_SKELETON_HEIGHT = 'h-[180px]';

export function HeroOutcomeRow({ targetHit, stopSaved, loading = false }: HeroOutcomeRowProps) {
  if (loading) {
    return (
      <div className="grid grid-cols-1 md:grid-cols-2 gap-3.5">
        <Skeleton className={HERO_SKELETON_HEIGHT} />
        <Skeleton className={HERO_SKELETON_HEIGHT} />
      </div>
    );
  }

  if (targetHit === null && stopSaved === null) {
    return null;
  }

  // Single-card states stretch full width so the section doesn't look
  // half-empty when only one of the two outcomes exists yet.
  // Two-card state uses a 2-col grid on md+, stacked on mobile.
  const bothPresent = targetHit !== null && stopSaved !== null;
  const gridClass = bothPresent
    ? 'grid grid-cols-1 md:grid-cols-2 gap-3.5'
    : 'grid grid-cols-1';

  return (
    <div className={gridClass}>
      {targetHit && <HeroOutcomeCard outcome={targetHit} variant="target_hit" />}
      {stopSaved && <HeroOutcomeCard outcome={stopSaved} variant="stop_saved" />}
    </div>
  );
}
