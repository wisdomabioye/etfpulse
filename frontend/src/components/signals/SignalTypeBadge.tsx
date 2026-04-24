import type { SignalType } from '../../api/types';
import { formatSignalType } from '../../lib/format';

interface SignalTypeBadgeProps {
  type: SignalType;
  size?: 'sm' | 'md';
}

/**
 * Outline pill showing the signal type label ("Flow Anomaly", "Magnitude", …).
 *
 * Matches mock's TypeBadge — transparent bg, border-2 outline, uppercase
 * mono text. Quiet by design; asset + confidence badges carry color weight.
 */
export function SignalTypeBadge({ type, size = 'md' }: SignalTypeBadgeProps) {
  const paddingClass = size === 'sm' ? 'px-[7px] py-[2px] text-[10px]' : 'px-[9px] py-[3px] text-[11px]';
  const radiusClass = size === 'sm' ? 'rounded-[4px]' : 'rounded-[5px]';

  return (
    <span
      className={`inline-flex items-center font-mono font-medium tracking-[0.02em] uppercase text-text-2 border border-border-2 ${paddingClass} ${radiusClass}`}
    >
      {formatSignalType(type)}
    </span>
  );
}
