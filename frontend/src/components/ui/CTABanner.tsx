import type { ReactNode } from 'react';

interface CTABannerProps {
  title: string;
  /** Optional sub-line; muted text below the title. */
  hint?: string;
  /** Right-aligned action — typically a Button. */
  action: ReactNode;
  className?: string;
}

/**
 * Bordered banner row — title + hint on the left, action on the right.
 * Matches the detail page's "Continue the discussion" CTA. Will also back
 * tier-upsell and bot-connect prompts.
 *
 * Wraps to column layout at narrow widths (`flex-wrap`) so the action
 * doesn't collide with a long title on mobile.
 */
export function CTABanner({ title, hint, action, className = '' }: CTABannerProps) {
  return (
    <div
      className={`border border-border-2 rounded-lg bg-bg-2 px-6 py-5 flex items-center justify-between gap-4 flex-wrap ${className}`.trim()}
    >
      <div>
        <div className="text-[14px] font-semibold text-text-1 mb-1">{title}</div>
        {hint && <div className="text-[12px] text-text-3">{hint}</div>}
      </div>
      {action}
    </div>
  );
}
