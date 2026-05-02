/**
 * Generic colored pill — the chrome backing `RegimeBadge` + `PostureBadge`.
 *
 * Both regime/posture surfaces want the same visual: a rounded mono-uppercase
 * pill with a colored border, a tinted background, and a small leading dot.
 * Only the displayed text, the color, and the size differ. This primitive
 * holds the shared layout so neither badge has to. New badge variants (e.g.
 * a future "tier" pill on the user profile) can reuse this directly.
 *
 * Lives in `components/regime/` because that's where its only consumers are
 * today; promote to `components/ui/` if a non-regime caller appears.
 */

interface ColoredPillProps {
  label: string;
  /** A CSS color value (e.g. `var(--color-pos)` or `#22c55e`). */
  color: string;
  /** "sm" = TopNav-friendly inline pill (compact, smaller dot).
   *  "md" = card hero (larger pill). */
  size?: 'sm' | 'md';
  /** Native browser tooltip. */
  title?: string;
  className?: string;
}

export function ColoredPill({
  label,
  color,
  size = 'sm',
  title,
  className = '',
}: ColoredPillProps) {
  const sizeClass =
    size === 'md'
      ? 'text-[12px] px-2.5 py-1 gap-2'
      : 'text-[11px] px-2 py-0.5 gap-1.5';
  const dotSize = size === 'md' ? 'w-1.5 h-1.5' : 'w-1 h-1';

  return (
    <span
      className={`inline-flex items-center rounded-full border font-mono uppercase tracking-[0.08em] ${sizeClass} ${className}`.trim()}
      style={{
        color,
        borderColor: color,
        // Subtle tinted background — pill reads as a state indicator,
        // not a clickable button. 15% of the color over transparent.
        backgroundColor: `color-mix(in oklab, ${color} 15%, transparent)`,
      }}
      title={title}
    >
      <span
        className={`inline-block rounded-full ${dotSize}`}
        style={{ background: color }}
        aria-hidden
      />
      {label}
    </span>
  );
}
