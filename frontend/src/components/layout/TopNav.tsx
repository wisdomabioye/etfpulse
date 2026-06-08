import { useState } from 'react';
import { NavLink, useLocation } from 'react-router-dom';
import { useDashboardStats } from '../../api/queries';
import { narrowRegime } from '../../lib/format';
import type { MarketRegime } from '../../api/types';
import { RegimeBadge } from '../regime/RegimeBadge';
import { LivePulse } from './LivePulse';
import { Logo } from './Logo';
import { PriceStrip } from './PriceStrip';

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
  // Analytics (Stage 8-P10) — diagnostic breakdown over the same data
  // /track-record surfaces. Placed AFTER Track Record so the nav reads
  // narrowest-to-widest: feed → list → breakdown → regime context.
  { label: 'Analytics', to: '/analytics', matches: ['/analytics'] },
  // Regime went live in Stage 7-P8 — RegimeBadge below the link surfaces
  // the current classification inline so users can see the regime without
  // navigating. The "Regime" string itself is also a link to /regime.
  { label: 'Regime', to: '/regime', matches: ['/regime'] },
  // Methodology (PR P2.1) discoverable from the Footer "How it works"
  // link only — a 6th TopNav item at md (768px) breakpoint risks
  // overflow given PriceStrip + LivePulse already share the row, and
  // the page is a once-or-twice reference read, not a primary nav
  // destination.
  // Trade (Stage 09 / PR D.4–D.5) — entry point to the SoDEX execution
  // surface. Placed last so the nav reads read-only-first → action-last
  // and the CTA sits where users instinctively scan for a "do something"
  // affordance. `matches` includes `/login` because the auth fallback
  // (`<Execute>` → `<Navigate to="/login">` when unauthed) is part of the
  // same logical surface — the user thinks "I'm trying to trade," not
  // "I'm on the login page". Without matching /login, the nav would lose
  // its active state mid-auth-flow.
  { label: 'Trade', to: '/execute', matches: ['/execute', '/login'] },
];

/**
 * Responsive nav.
 *
 * Desktop (≥md, 768px): horizontal row of links + LivePulse on the right.
 *
 * Mobile (<md): nav links collapse into a hamburger toggle. Logo + LivePulse
 * stay visible at all times so the user always knows where they are and
 * whether the backend is reachable. Opening the menu pushes a stacked link
 * panel below the nav row (accordion style — no fixed overlay so it can't
 * clip against the page's rounded container). Auto-closes when the route
 * changes (link tap or browser-back), so the user doesn't have to tap the
 * close button after picking a destination.
 *
 * Adding a nav item: append to `NAV_ITEMS` — desktop row and mobile panel
 * both render from the same list, so there's one place to edit.
 */
export function TopNav() {
  const location = useLocation();
  const stats = useDashboardStats();
  const [menuOpen, setMenuOpen] = useState(false);

  // Narrow the unknown-shaped string from the dashboard endpoint into the
  // strict MarketRegime literal — see `narrowRegime` for semantics. Cold
  // boot (no snapshot yet) and out-of-enum drift both collapse to null,
  // which hides the badge rather than crashing the navbar.
  const currentRegime = narrowRegime(stats.data?.current_regime);

  const closeMenu = () => setMenuOpen(false);

  const isActive = (item: NavItem): boolean => {
    if (!item.to) return false;
    if (item.matches?.some((m) => location.pathname.startsWith(m))) return true;
    return location.pathname === item.to;
  };

  return (
    <nav className="border-b border-border-2">
      <div className="flex items-center justify-between px-6 sm:px-8 py-4">
        <Logo size={15} />

        {/* Desktop — horizontal links. Hidden below md so the mobile bar
            doesn't compete for space with the hamburger. */}
        <div className="hidden md:flex items-center gap-4">
          <div className="flex items-center gap-1">
            {NAV_ITEMS.map((item) => (
              <NavLinkItem
                key={item.label}
                item={item}
                active={isActive(item)}
                regime={currentRegime}
                onNavigate={closeMenu}
              />
            ))}
          </div>
          <span className="inline-block w-px h-4 bg-border-2" aria-hidden />
          <PriceStrip />
          <span className="inline-block w-px h-4 bg-border-2" aria-hidden />
          <LivePulse />
        </div>

        {/* Mobile — PriceStrip + LivePulse stay visible, plus hamburger toggle.
            Strip sits before LivePulse so prices read left-to-right with the
            same priority order as desktop. Hidden on very narrow viewports
            (< sm) where the hamburger and brand already crowd the row. */}
        <div className="flex md:hidden items-center gap-3">
          <span className="hidden sm:inline-flex">
            <PriceStrip />
          </span>
          <LivePulse />
          <button
            type="button"
            onClick={() => setMenuOpen((open) => !open)}
            aria-label={menuOpen ? 'Close menu' : 'Open menu'}
            aria-expanded={menuOpen}
            aria-controls="mobile-nav-panel"
            className="inline-flex items-center justify-center w-9 h-9 rounded-md text-text-2 hover:text-text-1 hover:bg-bg-3 transition-colors"
          >
            {menuOpen ? <CloseIcon /> : <HamburgerIcon />}
          </button>
        </div>
      </div>

      {/* Mobile panel — accordion below the nav row. Renders only when
          open so screen readers don't see hidden links. */}
      {menuOpen && (
        <div
          id="mobile-nav-panel"
          className="md:hidden border-t border-border-2 px-4 py-3"
        >
          <ul className="flex flex-col gap-1">
            {NAV_ITEMS.map((item) => (
              <li key={item.label}>
                <NavLinkItem
                  item={item}
                  active={isActive(item)}
                  regime={currentRegime}
                  onNavigate={closeMenu}
                  mobile
                />
              </li>
            ))}
          </ul>
        </div>
      )}
    </nav>
  );
}

interface NavLinkItemProps {
  item: NavItem;
  active: boolean;
  regime: MarketRegime | null;
  /** Called when the user activates the link — used by the mobile panel
   *  to close itself after navigation. Desktop callers pass the same
   *  callback (it's idempotent — setMenuOpen(false) when already false
   *  is a no-op). */
  onNavigate: () => void;
  /** Mobile variant uses block layout + larger touch target. */
  mobile?: boolean;
}

/** One nav entry — used by both the desktop row and the mobile panel so
 *  the link rendering (active state, RegimeBadge, "Coming soon" disabled
 *  state) lives in one place. */
function NavLinkItem({
  item,
  active,
  regime,
  onNavigate,
  mobile = false,
}: NavLinkItemProps) {
  const baseClasses = mobile
    ? 'block w-full text-[14px] px-3 py-2.5 rounded-md font-medium'
    : 'inline-flex items-center gap-2 text-[13px] px-3 py-1.5 rounded-md font-medium transition-colors';

  if (item.to === null) {
    return (
      <span
        className={`${baseClasses} text-text-4 cursor-not-allowed select-none`}
        title="Coming soon"
      >
        {item.label}
      </span>
    );
  }

  // Inline regime badge sits to the RIGHT of the "Regime" link text
  // (not on top, so the click target stays the link text itself). When
  // regime data hasn't loaded yet OR the backend reports null, the badge
  // is omitted — no skeleton shimmer in the navbar.
  return (
    <NavLink
      to={item.to}
      onClick={onNavigate}
      className={`${baseClasses} ${
        active ? 'text-text-1 bg-bg-3' : 'text-text-3 hover:text-text-1'
      } ${mobile ? 'flex items-center gap-2' : ''}`}
    >
      <span>{item.label}</span>
      {item.label === 'Regime' && regime !== null && (
        <RegimeBadge regime={regime} size="sm" />
      )}
    </NavLink>
  );
}

function HamburgerIcon() {
  return (
    <svg
      width="18"
      height="18"
      viewBox="0 0 18 18"
      fill="none"
      aria-hidden
    >
      <path
        d="M2 4.5h14M2 9h14M2 13.5h14"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
      />
    </svg>
  );
}

function CloseIcon() {
  return (
    <svg
      width="18"
      height="18"
      viewBox="0 0 18 18"
      fill="none"
      aria-hidden
    >
      <path
        d="M4 4l10 10M14 4L4 14"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
      />
    </svg>
  );
}
