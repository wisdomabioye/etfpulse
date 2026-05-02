import type { MarketRegime } from '../../api/types';
import { formatRegime, regimeColor } from '../../lib/format';
import { ColoredPill } from './ColoredPill';

interface RegimeBadgeProps {
  regime: MarketRegime;
  /** Forwarded to ColoredPill — see its prop docs. */
  size?: 'sm' | 'md';
  className?: string;
}

/**
 * Surfaces the current Wyckoff regime as a colored pill.
 *   - TopNav     → `size="sm"` next to the Regime link
 *   - RegimeCard → `size="md"` in the hero block
 *
 * Color and label both flow from the regime value via shared formatters
 * in `lib/format.ts` — single source of truth for the regime ↔ hue map.
 */
export function RegimeBadge({
  regime,
  size = 'sm',
  className = '',
}: RegimeBadgeProps) {
  return (
    <ColoredPill
      label={formatRegime(regime)}
      color={regimeColor(regime)}
      size={size}
      title={`Current regime: ${formatRegime(regime)}`}
      className={className}
    />
  );
}
