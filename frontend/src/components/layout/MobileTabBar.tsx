import { NavLink, useLocation } from 'react-router-dom';

interface Tab {
  label: string;
  to: string;
  /** SVG path `d` for the 24×24 line icon. */
  icon: string;
  /** Extra route prefixes that should light this tab. */
  matches?: string[];
}

// Four primary destinations. "Proof" → /track-record (the merged proof
// surface); full nav (Regime/Learn) lives in the desktop row + mobile menu.
const TABS: Tab[] = [
  { label: 'Home', to: '/', icon: 'M3 11l9-8 9 8M5 10v10h14V10' },
  { label: 'Signals', to: '/signals', icon: 'M3 12h4l3-8 4 16 3-8h4', matches: ['/signals'] },
  { label: 'Proof', to: '/track-record', icon: 'M4 20V10M10 20V4M16 20v-7M22 20H2', matches: ['/track-record'] },
  { label: 'Trade', to: '/execute', icon: 'M12 2v20M5 9l7-7 7 7', matches: ['/execute', '/login'] },
];

/**
 * Fixed bottom tab bar for mobile (hidden ≥md). Ported from the prototype's
 * `MobileTabBar` — 4 primary destinations, active tab in amber. Respects the
 * iOS safe-area inset so it clears the home indicator.
 */
export function MobileTabBar() {
  const location = useLocation();
  return (
    <nav
      className="md:hidden fixed bottom-0 left-0 right-0 z-[60] bg-bg-1/95 backdrop-blur-md border-t border-line-2"
      style={{ padding: '8px 8px calc(8px + env(safe-area-inset-bottom))' }}
      aria-label="Primary"
    >
      <div className="flex justify-around">
        {TABS.map((t) => (
          <NavLink
            key={t.label}
            to={t.to}
            end={t.to === '/'}
            className={({ isActive }) => {
              const active =
                isActive || (t.matches?.some((m) => location.pathname.startsWith(m)) ?? false);
              return `flex flex-col items-center gap-1 px-3.5 py-1 min-w-[44px] ${
                active ? 'text-acc-hi' : 'text-t3'
              }`;
            }}
          >
            <svg
              width="20"
              height="20"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.8"
              strokeLinecap="round"
              strokeLinejoin="round"
              aria-hidden
            >
              <path d={t.icon} />
            </svg>
            <span className="font-mono text-[9px] tracking-[0.05em]">{t.label}</span>
          </NavLink>
        ))}
      </div>
    </nav>
  );
}
