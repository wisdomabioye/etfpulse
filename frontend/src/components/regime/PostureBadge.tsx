import type { SignalPosture } from '../../api/types';
import { formatPosture, postureColor } from '../../lib/format';
import { ColoredPill } from './ColoredPill';

interface PostureBadgeProps {
  posture: SignalPosture;
  className?: string;
}

/**
 * Companion to `RegimeBadge` — surfaces "how aggressively the engine is
 * currently firing signals" alongside the regime read. Single size today
 * (only used inside RegimeCard's stat row); add a `size` prop forwarding
 * to ColoredPill if a TopNav-sized variant ever appears.
 */
export function PostureBadge({ posture, className = '' }: PostureBadgeProps) {
  return (
    <ColoredPill
      label={formatPosture(posture)}
      color={postureColor(posture)}
      title={`Signal posture: ${formatPosture(posture)}`}
      className={className}
    />
  );
}
