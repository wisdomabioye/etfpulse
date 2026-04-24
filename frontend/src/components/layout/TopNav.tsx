import { NavLink, useLocation } from 'react-router-dom';
import { Logo } from './Logo';

interface NavItem {
  label: string;
  /** Target route, or null for "coming soon" items. */
  to: string | null;
  matches?: string[];
}

const NAV_ITEMS: NavItem[] = [
  { label: 'Signals', to: '/signals', matches: ['/signals'] },
  { label: 'Track Record', to: null },
  { label: 'Regime', to: null },
];

export function TopNav() {
  const location = useLocation();

  const isActive = (item: NavItem): boolean => {
    if (!item.to) return false;
    if (item.matches?.some((m) => location.pathname.startsWith(m))) return true;
    return location.pathname === item.to;
  };

  return (
    <nav className="flex items-center justify-between px-6 sm:px-8 py-4 border-b border-border-2">
      <Logo size={15} />
      <div className="flex items-center gap-1">
        {NAV_ITEMS.map((item) => {
          if (item.to === null) {
            return (
              <span
                key={item.label}
                className="text-[13px] px-3 py-1.5 text-text-4 cursor-not-allowed select-none"
                title="Coming soon"
              >
                {item.label}
              </span>
            );
          }
          const active = isActive(item);
          return (
            <NavLink
              key={item.label}
              to={item.to}
              className={`text-[13px] px-3 py-1.5 rounded-md font-medium transition-colors ${
                active ? 'text-text-1 bg-bg-3' : 'text-text-3 hover:text-text-1'
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
