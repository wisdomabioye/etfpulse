import type { SuggestedAction } from '../../api/types';
import { actionMeta } from '../../lib/colors';
import { colorMix, cssVar } from '../../lib/colorMix';

interface ActionTagProps {
  action: SuggestedAction;
  size?: 'sm' | 'md';
}

/**
 * Suggested-action chip (Long ▲ / Short ▼ / Wait ■) — ported from the
 * prototype's `ActionTag`, tinted by `actionMeta` (win / loss / warn). The
 * tone is data-driven → inline `colorMix` / `cssVar`; the arrow is
 * `aria-hidden` since the label already names the action.
 */
export function ActionTag({ action, size = 'md' }: ActionTagProps) {
  const meta = actionMeta(action);
  const sizeCls = size === 'sm' ? 'px-1.5 py-px text-[10px]' : 'px-2 py-0.5 text-[11px]';

  return (
    <span
      className={`inline-flex items-center gap-1 font-mono font-semibold rounded-sm ${sizeCls}`}
      style={{
        color: cssVar(meta.tone),
        border: `1px solid ${colorMix(meta.tone, 30)}`,
        background: colorMix(meta.tone, 8),
      }}
    >
      <span aria-hidden="true">{meta.arrow}</span>
      {meta.label}
    </span>
  );
}
