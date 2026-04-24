import { NavLink, useLocation } from 'react-router-dom';
import { Logo } from './Logo';

interface NavItem {
  label: string;
  /** Target route, or null for "coming soon" (Track Record / Regime). */
  to: string | null;
  /** Substrings of pathname that count as "this item is active". Used for
   * /signals/:id to light up the Signals link too. */
  matches?: string[];
}

const NAV_ITEMS: NavItem[] = [
  { label: 'Signals', to: '/signals', matches: ['/signals'] },
  { label: 'Track Record', to: null }, // Stage 8
  { label: 'Regime', to: null }, // Stage 7
];

/**
 * Sticky top navigation with glass blur and bottom border.
 *
 * Matches mock primitives.jsx TopNav: Logo on the left, 4 nav items on the
 * right. Track Record + Regime render as disabled with a muted "soon" label
 * — pages exist in the plan but are Stage 7/8 scope.
 *
 * Active-state: uses react-router's `useLocation` to highlight the matching
 * item. `/signals/:id` (detail page) also lights up the Signals link via
 * the `matches` prefix check.
 */
export function TopNav() {
  const location = useLocation();

  const isActive = (item: NavItem): boolean => {
    if (!item.to) return false;
    if (item.matches?.some((m) => location.pathname.startsWith(m))) return true;
    return location.pathname === item.to;
  };

  return (
    <nav className="sticky top-0 z-10 flex items-center justify-between px-5 md:px-7 py-4 border-b border-border-2 bg-bg-1/90 backdrop-blur-md">
      <Logo size={15} />

      <div className="flex items-center gap-1">
        {NAV_ITEMS.map((item) => {
          if (item.to === null) {
            // "Coming soon" — visible but disabled
            return (
              <span
                key={item.label}
                className="inline-flex items-center gap-1.5 text-[13px] px-3 py-1.5 rounded-[5px] text-text-4 cursor-not-allowed select-none"
                title="Coming soon"
              >
                {item.label}
                <span className="font-mono text-[9px] uppercase tracking-wider text-text-4 border border-border-2 rounded px-1 py-[1px]">
                  soon
                </span>
              </span>
            );
          }

          const active = isActive(item);
          return (
            <NavLink
              key={item.label}
              to={item.to}
              className={`text-[13px] px-3 py-1.5 rounded-[5px] font-medium no-underline hover:no-underline transition-colors ${
                active ? 'text-text-1 bg-bg-3' : 'text-text-3 hover:text-text-2'
              }`}
            >
              {item.label}
            </NavLink>
          );
        })}
      </div>
    </nav>
  );
}
