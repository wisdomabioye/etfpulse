import { NavLink, useLocation } from 'react-router-dom';
import { useDashboardStats } from '../../api/queries';
import { narrowRegime } from '../../lib/format';
import { RegimeBadge } from '../regime/RegimeBadge';
import { LivePulse } from './LivePulse';
import { Logo } from './Logo';

interface NavItem {
  label: string;
  /** Target route, or null for "coming soon" items. */
  to: string | null;
  matches?: string[];
}

const NAV_ITEMS: NavItem[] = [
  { label: 'Signals', to: '/signals', matches: ['/signals'] },
  // Track Record went live in Stage 8-P6 (closes issue #44 + the
  // SignalOutcome chain through P1–P5). The page surfaces the same
  // hit_rate_72h carried on the home tile, plus per-asset/per-type
  // filtered views.
  { label: 'Track Record', to: '/track-record', matches: ['/track-record'] },
  // Regime went live in Stage 7-P8 — RegimeBadge below the link surfaces
  // the current classification inline so users can see the regime without
  // navigating. The "Regime" string itself is also a link to /regime.
  { label: 'Regime', to: '/regime', matches: ['/regime'] },
];

export function TopNav() {
  const location = useLocation();
  const stats = useDashboardStats();

  // Narrow the unknown-shaped string from the dashboard endpoint into the
  // strict MarketRegime literal — see `narrowRegime` for semantics. Cold
  // boot (no snapshot yet) and out-of-enum drift both collapse to null,
  // which hides the badge rather than crashing the navbar.
  const currentRegime = narrowRegime(stats.data?.current_regime);

  const isActive = (item: NavItem): boolean => {
    if (!item.to) return false;
    if (item.matches?.some((m) => location.pathname.startsWith(m))) return true;
    return location.pathname === item.to;
  };

  return (
    <nav className="flex items-center justify-between px-6 sm:px-8 py-4 border-b border-border-2">
      <Logo size={15} />
      <div className="flex items-center gap-4">
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
          // Inline regime badge sits to the RIGHT of the "Regime" link
          // text (not on top of the label, so the click target stays the
          // link text itself). When regime data hasn't loaded yet OR the
          // backend reports null, the badge is omitted — no skeleton
          // shimmer in the navbar. The `currentRegime !== null` check is
          // load-bearing for TS narrowing inside the JSX expression.
          return (
            <NavLink
              key={item.label}
              to={item.to}
              className={`inline-flex items-center gap-2 text-[13px] px-3 py-1.5 rounded-md font-medium transition-colors ${
                active ? 'text-text-1 bg-bg-3' : 'text-text-3 hover:text-text-1'
              }`}
            >
              {item.label}
              {item.label === 'Regime' && currentRegime !== null && (
                <RegimeBadge regime={currentRegime} size="sm" />
              )}
            </NavLink>
          );
        })}
        </div>
        <span className="hidden sm:inline-block w-px h-4 bg-border-2" aria-hidden />
        <LivePulse />
      </div>
    </nav>
  );
}
