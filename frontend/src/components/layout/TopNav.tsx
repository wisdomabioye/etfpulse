import { useState } from 'react';
import { Link, NavLink, useLocation, useNavigate } from 'react-router-dom';

import { useAuth } from '../../auth/useAuth';
import { useWalletMe } from '../../hooks/useExecution';
import { colorMix } from '../../lib/colorMix';
import { Button, LiveDot, Logo } from '../ui';
import { PriceStrip } from './PriceStrip';
import { RegimeGlance } from './RegimeGlance';

interface NavItem {
  label: string;
  to: string;
  /** Extra route prefixes that light this item (e.g. /login for Trade). */
  matches?: string[];
  /** Requires a connected wallet — gets a paper/live status dot. */
  auth?: boolean;
}

// Grouped nav (prototype IA): Proof = the merged track-record surface,
// Learn = methodology. Analytics folds into Proof (R5).
const NAV_ITEMS: NavItem[] = [
  { label: 'Signals', to: '/signals', matches: ['/signals'] },
  { label: 'Proof', to: '/track-record', matches: ['/track-record', '/analytics'] },
  { label: 'Regime', to: '/regime', matches: ['/regime'] },
  { label: 'Trade', to: '/execute', matches: ['/execute', '/login'], auth: true },
  { label: 'Learn', to: '/methodology', matches: ['/methodology'] },
];

/**
 * Two-row sticky app header — reskinned (R3) to the prototype's shell.
 *   Row 1: live dot + spot price strip · regime glance + posture.
 *   Row 2: brand + grouped nav · account CTA + mobile menu toggle.
 *
 * The prototype's dev-only settings gear (tweak panel) is dropped (D4). The
 * faked spot walk is replaced by the real `PriceStrip`. Account state reads
 * the real `useAuth`; mobile keeps the accessible accordion for the full nav
 * (the bottom `MobileTabBar` carries the 4 primary destinations).
 */
export function TopNav() {
  const location = useLocation();
  const navigate = useNavigate();
  const { isAuthed } = useAuth();
  const [menuOpen, setMenuOpen] = useState(false);
  const closeMenu = () => setMenuOpen(false);

  const isActive = (item: NavItem) =>
    item.matches?.some((m) => location.pathname.startsWith(m)) ?? location.pathname === item.to;

  return (
    <header className="sticky top-0 z-50 bg-bg-1/[0.88] backdrop-blur-md border-b border-line-2">
      {/* row 1: status */}
      <div className="flex items-center justify-between px-6 py-[7px] border-b border-line-1">
        <div className="flex items-center gap-4">
          <LiveDot />
          <PriceStrip />
        </div>
        <div className="hidden sm:block">
          <RegimeGlance />
        </div>
      </div>

      {/* row 2: brand + nav + account */}
      <nav className="flex items-center justify-between px-6 py-3">
        <div className="flex items-center gap-7">
          <Logo size={16} onClick={() => navigate('/')} />
          <div className="hidden md:flex gap-0.5">
            {NAV_ITEMS.map((item) => (
              <NavItemLink key={item.label} item={item} active={isActive(item)} onNavigate={closeMenu} />
            ))}
          </div>
        </div>

        <div className="flex items-center gap-2.5">
          <div className="hidden md:block">
            {isAuthed ? (
              <AccountChip />
            ) : (
              <Button as="link" to="/login" variant="primary" size="sm">
                Connect wallet
              </Button>
            )}
          </div>
          <button
            type="button"
            onClick={() => setMenuOpen((o) => !o)}
            aria-label={menuOpen ? 'Close menu' : 'Open menu'}
            aria-expanded={menuOpen}
            aria-controls="mobile-nav-panel"
            className="md:hidden inline-flex items-center justify-center w-9 h-9 rounded-sm border border-line-2 text-t2 hover:text-t1 hover:bg-bg-3 transition-colors"
          >
            <MenuIcon open={menuOpen} />
          </button>
        </div>
      </nav>

      {/* mobile accordion — full nav list (incl. Regime/Learn beyond the tab bar) */}
      {menuOpen && (
        <div id="mobile-nav-panel" className="md:hidden border-t border-line-2 px-4 py-3">
          <ul className="flex flex-col gap-1">
            {NAV_ITEMS.map((item) => (
              <li key={item.label}>
                <NavItemLink item={item} active={isActive(item)} onNavigate={closeMenu} mobile />
              </li>
            ))}
            <li className="pt-2">
              <Button as="link" to={isAuthed ? '/execute' : '/login'} variant="primary" size="sm" full>
                {isAuthed ? 'Account' : 'Connect wallet'}
              </Button>
            </li>
          </ul>
        </div>
      )}
    </header>
  );
}

interface NavItemLinkProps {
  item: NavItem;
  active: boolean;
  onNavigate: () => void;
  mobile?: boolean;
}

function NavItemLink({ item, active, onNavigate, mobile = false }: NavItemLinkProps) {
  const base = mobile
    ? 'flex items-center gap-2 w-full text-[14px] px-3 py-2.5 rounded-sm font-medium'
    : 'flex items-center gap-[7px] text-[13px] px-[13px] py-[7px] rounded-sm transition-colors duration-[var(--dur-1)]';
  return (
    <NavLink
      to={item.to}
      onClick={onNavigate}
      className={`${base} ${active ? 'text-t1 bg-bg-3 font-semibold' : 'text-t3 hover:text-t1 font-medium'}`}
    >
      {item.label}
      {item.auth && <span className="w-[5px] h-[5px] rounded-full bg-warn" title="paper mode" />}
    </NavLink>
  );
}

/** Connected-account cluster — paper/live mode pill + truncated wallet
 *  address (prototype's distinctive header element). Only mounts when authed
 *  (so `useWalletMe` never fires on public pages). Clicking it opens a small
 *  dropdown with the full address (copyable), the mode, an Execute link, and
 *  a Disconnect action.
 *
 *  Disconnect = clear the app session (JWT, via `useAuth().logout()`, which
 *  also redirects to /login) + best-effort disconnect the wallet at the
 *  wagmi level. The `WagmiProvider` only wraps /login + /execute, so this
 *  global component can't call wagmi hooks; instead it dynamically imports
 *  `disconnectWallet()` (keeps the wagmi bundle out of the main chunk and
 *  works from any page). */
function AccountChip() {
  const me = useWalletMe();
  const { logout } = useAuth();
  const [open, setOpen] = useState(false);
  const [copied, setCopied] = useState(false);
  const addr = me.data?.wallet_address ?? null;
  const paper = me.data?.paper_trade ?? true;
  const token = paper ? '--warn' : '--win';

  async function handleDisconnect() {
    setOpen(false);
    // Clear the JWT FIRST — this is the snappy sign-out (and redirects to
    // /login); it must not wait on the wallet layer. A Telegram-only session
    // has no connected wallet, so awaiting the wagmi import first would just
    // load the ~1.5 MB wagmi chunk to perform a no-op before signing out.
    logout();
    // Then best-effort disconnect the wallet at the wagmi level (dynamic
    // import keeps wagmi out of the main bundle). Runs after logout; the
    // closure survives this component unmounting. Wrapped so a wagmi failure
    // never matters — the JWT is already cleared.
    try {
      const { disconnectWallet } = await import('../../lib/wagmi');
      await disconnectWallet();
    } catch {
      /* wallet layer unavailable — already signed out via logout() above */
    }
  }

  async function copyAddr() {
    if (!addr) return;
    try {
      await navigator.clipboard.writeText(addr);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1200);
    } catch {
      /* clipboard blocked (insecure context / permissions) — no-op */
    }
  }

  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label="Account menu"
        className="flex items-center gap-2.5 cursor-pointer"
      >
        <span
          className={`font-mono text-[11px] px-[9px] py-1 rounded-sm border ${paper ? 'bg-warn-soft text-warn' : 'bg-win-soft text-win'}`}
          style={{ borderColor: colorMix(token, 30) }}
        >
          {paper ? '◐ PAPER' : '● LIVE'}
        </span>
        {addr && (
          <span className="font-mono text-[12px] text-t2">
            {addr.slice(0, 4)}…{addr.slice(-3)}
          </span>
        )}
        <svg width="10" height="10" viewBox="0 0 10 10" className="text-t3" aria-hidden>
          <path
            d={open ? 'M2 6.5L5 3.5l3 3' : 'M2 3.5L5 6.5l3-3'}
            stroke="currentColor"
            strokeWidth="1.4"
            fill="none"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      </button>
      {open && (
        <>
          {/* click-away backdrop */}
          <button
            type="button"
            aria-hidden
            tabIndex={-1}
            onClick={() => setOpen(false)}
            className="fixed inset-0 z-40 cursor-default"
          />
          <div
            role="menu"
            className="absolute right-0 top-[calc(100%+8px)] z-50 w-[264px] bg-bg-2 border border-line-3 rounded-md p-3.5"
            style={{ boxShadow: 'var(--shadow-2)' }}
          >
            <div className="font-mono text-[9.5px] text-t4 tracking-[0.1em] uppercase mb-2">
              Connected wallet
            </div>
            {addr ? (
              <button
                type="button"
                onClick={copyAddr}
                title="Copy address"
                className="w-full text-left font-mono text-[12px] text-t1 break-all hover:text-acc-hi"
              >
                {addr}
                <span className="ml-1 text-t4">{copied ? '· copied' : '· copy'}</span>
              </button>
            ) : (
              <div className="text-[12px] text-t3">No wallet bound (Telegram session).</div>
            )}
            <div className="flex items-center gap-2 mt-3">
              <span
                className={`font-mono text-[10px] px-1.5 py-0.5 rounded-sm border ${paper ? 'bg-warn-soft text-warn' : 'bg-win-soft text-win'}`}
                style={{ borderColor: colorMix(token, 30) }}
              >
                {paper ? '◐ PAPER' : '● LIVE'}
              </span>
              <span className="text-[11px] text-t3">
                {paper ? 'Simulated fills — no real funds' : 'Live trading'}
              </span>
            </div>
            <div className="flex flex-col gap-2 mt-3.5 pt-3 border-t border-line-2">
              <Link
                to="/execute"
                onClick={() => setOpen(false)}
                className="text-[12px] text-t2 hover:text-t1"
              >
                Open Execute →
              </Link>
              <button
                type="button"
                onClick={handleDisconnect}
                className="text-left text-[12px] text-loss hover:brightness-110"
              >
                Disconnect
              </button>
            </div>
          </div>
        </>
      )}
    </div>
  );
}

function MenuIcon({ open }: { open: boolean }) {
  return (
    <svg width="18" height="18" viewBox="0 0 18 18" fill="none" aria-hidden>
      <path
        d={open ? 'M4 4l10 10M14 4L4 14' : 'M2 4.5h14M2 9h14M2 13.5h14'}
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
      />
    </svg>
  );
}
