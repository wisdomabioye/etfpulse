import type { SignalType } from '../../api/types';
import { detectorColorToken } from '../../lib/colors';
import { cssVar } from '../../lib/colorMix';

interface DetectorIconProps {
  type: SignalType;
  /** Square px size of the glyph. */
  size?: number;
  /** Stroke override; defaults to the detector's identity color token. */
  color?: string;
  className?: string;
}

/**
 * Coherent mini-glyph per detector — a 16×16 line icon set ported 1:1 from
 * the prototype's `DetectorIcon`. Stroke defaults to the detector's identity
 * color (`detectorColorToken`) so the icon matches its badge.
 *
 * Pure SVG, `aria-hidden` (the label beside it carries the meaning).
 */
export function DetectorIcon({ type, size = 12, color, className }: DetectorIconProps) {
  const stroke = color ?? cssVar(detectorColorToken(type));
  const common = {
    width: size,
    height: size,
    viewBox: '0 0 16 16',
    fill: 'none',
    stroke,
    strokeWidth: 1.6,
    strokeLinecap: 'round' as const,
    strokeLinejoin: 'round' as const,
    'aria-hidden': true,
    className,
  };
  switch (type) {
    case 'flow_anomaly':
      return (
        <svg {...common}>
          <path d="M1 9 L4 9 L6 4 L8 12 L9 9 L15 9" />
          <circle cx="8" cy="12" r="0.5" fill={stroke} />
        </svg>
      );
    case 'magnitude':
      return (
        <svg {...common}>
          <path d="M3 14 V7 M8 14 V2 M13 14 V10" />
        </svg>
      );
    case 'acceleration':
      return (
        <svg {...common}>
          <path d="M2 12 Q6 12 8 7 T14 3" />
          <path d="M11 3 H14 V6" />
        </svg>
      );
    case 'divergence':
      return (
        <svg {...common}>
          <path d="M2 8 H6 M10 8 H14" />
          <path d="M6 8 L9 4 M6 8 L9 12" />
        </svg>
      );
    case 'regime_shift':
      return (
        <svg {...common}>
          <path d="M3 5 H11 L9 3 M13 11 H5 L7 13" />
        </svg>
      );
  }
}
