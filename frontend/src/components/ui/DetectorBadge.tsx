import type { SignalType } from '../../api/types';
import { DETECTORS } from '../../lib/constants';
import { detectorColorToken } from '../../lib/colors';
import { colorMix, cssVar } from '../../lib/colorMix';
import { DetectorIcon } from './DetectorIcon';

interface DetectorBadgeProps {
  type: SignalType;
  size?: 'sm' | 'md';
  /** Hide the text label, leaving just the icon (compact rows). */
  showLabel?: boolean;
}

/**
 * Detector identity chip — icon + name, tinted in the detector's color
 * (9% fill, 35% border, solid text). Ported from the prototype's
 * `DetectorBadge`. Colors are data-driven (per detector) so the tint /
 * border / text come through inline `colorMix` / `cssVar`; layout is
 * utility classes. `sm` uses the terse label, `md` the full name.
 */
export function DetectorBadge({ type, size = 'md', showLabel = true }: DetectorBadgeProps) {
  const meta = DETECTORS[type];
  const token = detectorColorToken(type);
  const sizeCls =
    size === 'sm' ? 'px-[7px] py-px text-[10px] gap-[5px]' : 'px-[9px] py-[3px] text-[11px] gap-1.5';

  return (
    <span
      className={`inline-flex items-center font-mono font-medium uppercase tracking-[0.02em] rounded-sm ${sizeCls}`}
      style={{
        color: cssVar(token),
        border: `1px solid ${colorMix(token, 35)}`,
        background: colorMix(token, 9),
      }}
    >
      <DetectorIcon type={type} size={size === 'sm' ? 9 : 11} />
      {showLabel && (size === 'sm' ? meta.short : meta.label)}
    </span>
  );
}
