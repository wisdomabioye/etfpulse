import { confColorToken, confSoftToken } from '../../lib/colors';
import { colorMix, cssVar } from '../../lib/colorMix';

interface ConfidenceBadgeProps {
  /** Confidence 1–10. */
  value: number;
  size?: 'sm' | 'lg';
}

/**
 * Confidence chip — a colored dot + "N/10", tinted by the confidence ramp
 * (`confColorToken` / `confSoftToken`). Ported from the prototype's
 * `ConfidenceBadge`. `lg` is the stacked panel used on the detail page;
 * `sm` is the inline row chip. Ramp colors are data-driven → inline styles.
 */
export function ConfidenceBadge({ value, size = 'sm' }: ConfidenceBadgeProps) {
  const colorToken = confColorToken(value);
  const softToken = confSoftToken(value);
  const dot = cssVar(colorToken);

  if (size === 'lg') {
    return (
      <div
        className="inline-flex flex-col px-[14px] py-2.5 rounded-md"
        style={{
          background: cssVar(softToken),
          border: `1px solid ${colorMix(colorToken, 30)}`,
        }}
      >
        <div className="flex items-center gap-2">
          <span className="w-2 h-2 rounded-full" style={{ background: dot }} />
          <span className="font-mono tabular-nums text-[22px] font-semibold">
            {value}
            <span className="text-t3">/10</span>
          </span>
        </div>
        <span className="font-mono text-[9px] text-t3 tracking-[0.12em] uppercase mt-[3px]">
          confidence
        </span>
      </div>
    );
  }

  return (
    <span
      className="inline-flex items-center gap-[5px] px-[7px] py-0.5 rounded-sm font-mono tabular-nums text-[11px] font-semibold"
      style={{
        background: cssVar(softToken),
        border: `1px solid ${colorMix(colorToken, 28)}`,
      }}
    >
      <span className="w-[5px] h-[5px] rounded-full" style={{ background: dot }} />
      {value}
      <span className="text-t3 font-normal">/10</span>
    </span>
  );
}
