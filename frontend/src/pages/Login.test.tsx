/**
 * Login page regression tests (FE-fix.4).
 *
 * Pins the three fixes that landed in FE-fix.1–.3:
 *   - Visible CTAs: both Connect Wallet and Sign in with Ethereum
 *     buttons must use a Tailwind token that resolves to a defined
 *     CSS variable. The codebase convention is `bg-accent` (NOT
 *     `bg-accent-1`, which is undefined and rendered invisible).
 *   - Disconnect affordance: when the wallet IS connected, a
 *     Disconnect control must be present so the user has an inline
 *     way to disconnect without opening the AppKit modal.
 *
 * Strategy: mock the wagmi + AppKit hooks at module-resolution time
 * via `vi.mock`. The Login page never goes near a real wallet here;
 * we just exercise its rendering paths under controlled hook returns.
 */

import type { ReactNode } from 'react';

import { render, screen, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// Explicit return-type annotation on the auth mock so a later test
// can return `jwt: 'fake-jwt'` without TS narrowing to `jwt: null`.
interface AuthMockShape {
  jwt: string | null;
  isAuthed: boolean;
  login: (jwt: string) => void;
  logout: () => void;
}

const mockUseAccount = vi.fn();
const mockUseDisconnect = vi.fn();
const mockUseSignMessage = vi.fn(() => ({ signMessageAsync: vi.fn() }));
const mockUseAppKit = vi.fn(() => ({ open: vi.fn() }));
const mockUseAuth = vi.fn<() => AuthMockShape>(() => ({
  jwt: null,
  isAuthed: false,
  login: vi.fn(),
  logout: vi.fn(),
}));

vi.mock('wagmi', () => ({
  useAccount: () => mockUseAccount(),
  useDisconnect: () => mockUseDisconnect(),
  useSignMessage: () => mockUseSignMessage(),
}));

vi.mock('@reown/appkit/react', () => ({
  useAppKit: () => mockUseAppKit(),
}));

vi.mock('../auth/useAuth', () => ({
  useAuth: () => mockUseAuth(),
}));

vi.mock('../lib/wagmi', () => ({
  isWalletConnectAvailable: true,
}));

vi.mock('../auth/siwe', () => ({
  performSiweLogin: vi.fn(),
}));

// Import AFTER mocks so the Login module picks them up.
import { Login } from './Login';

function renderLogin(children: ReactNode = <Login />) {
  return render(<MemoryRouter initialEntries={['/login']}>{children}</MemoryRouter>);
}

// Set defaults BEFORE each test so hooks called during the first
// render don't see undefined returns and crash on destructuring.
// Individual tests override these via `.mockReturnValue(...)` as needed.
beforeEach(() => {
  mockUseAccount.mockReturnValue({ address: undefined, isConnected: false });
  mockUseDisconnect.mockReturnValue({ disconnect: vi.fn() });
  mockUseSignMessage.mockReturnValue({ signMessageAsync: vi.fn() });
  mockUseAppKit.mockReturnValue({ open: vi.fn() });
  // Explicit `string | null` for `jwt` so a later test can pass a
  // string without TS narrowing the mock return type to `{ jwt: null }`.
  const initial: {
    jwt: string | null;
    isAuthed: boolean;
    login: (jwt: string) => void;
    logout: () => void;
  } = {
    jwt: null,
    isAuthed: false,
    login: vi.fn(),
    logout: vi.fn(),
  };
  mockUseAuth.mockReturnValue(initial);
});

afterEach(() => {
  vi.clearAllMocks();
});

describe('Login CTA visibility (regression for invisible bg-accent-1 buttons)', () => {
  it('Connect Wallet button uses the solid bg-acc accent (R9: amber tokens)', () => {
    mockUseAccount.mockReturnValue({ address: undefined, isConnected: false });
    renderLogin();
    const btn = screen.getByRole('button', { name: /connect wallet/i });
    expect(btn).toBeInTheDocument();
    // The CTA must carry the SOLID accent fill, not the faint `bg-acc-soft`
    // tint (which would be near-invisible) — locks the visibility regression.
    expect(btn.className).toMatch(/\bbg-acc\b/);
    expect(btn.className).not.toMatch(/bg-acc-soft/);
  });

  it('Sign in with Ethereum button uses bg-acc when wallet is connected', () => {
    mockUseAccount.mockReturnValue({
      address: '0xabc0000000000000000000000000000000000def',
      isConnected: true,
    });
    renderLogin();
    const btn = screen.getByRole('button', { name: /sign in with ethereum/i });
    expect(btn).toBeInTheDocument();
    expect(btn.className).toMatch(/\bbg-acc\b/);
    expect(btn.className).not.toMatch(/bg-acc-soft/);
  });
});

describe('Disconnect affordance', () => {
  it('renders a Disconnect button when wallet is connected', () => {
    mockUseAccount.mockReturnValue({
      address: '0xabc0000000000000000000000000000000000def',
      isConnected: true,
    });
    renderLogin();
    expect(screen.getByRole('button', { name: /disconnect/i })).toBeInTheDocument();
  });

  it('does NOT render a Disconnect button when wallet is disconnected', () => {
    mockUseAccount.mockReturnValue({ address: undefined, isConnected: false });
    renderLogin();
    expect(screen.queryByRole('button', { name: /disconnect/i })).not.toBeInTheDocument();
  });

  it('clicking Disconnect calls wagmi useDisconnect().disconnect', () => {
    const disconnect = vi.fn();
    mockUseDisconnect.mockReturnValue({ disconnect });
    mockUseAccount.mockReturnValue({
      address: '0xabc0000000000000000000000000000000000def',
      isConnected: true,
    });
    renderLogin();
    fireEvent.click(screen.getByRole('button', { name: /disconnect/i }));
    expect(disconnect).toHaveBeenCalledOnce();
  });
});

describe('Authed-user bounce', () => {
  it('redirects an already-authed visitor away from /login', () => {
    mockUseAuth.mockReturnValue({
      jwt: 'fake-jwt',
      isAuthed: true,
      login: vi.fn(),
      logout: vi.fn(),
    });
    renderLogin();
    // Bounce target is /execute — no Login UI should be visible.
    expect(screen.queryByRole('button', { name: /connect wallet/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /sign in/i })).not.toBeInTheDocument();
  });
});

/**
 * SIG2X.3 — preserve the URL through the auth bounce.
 *
 * Tested at the pure-function level (the path resolver) since
 * asserting on react-router's `<Navigate>` effect in a memory
 * router is brittle. The resolver is the entire decision point;
 * if it returns the right string, Navigate routes correctly.
 */
import type { Location } from 'react-router-dom';

import { resolvePostLoginPath } from '../auth/postLoginPath';

// Build a Location via cast — react-router's Location type carries
// optional internal fields (`unstable_mask`, etc) that aren't worth
// satisfying in test fixtures. We only read the four documented
// fields, so a structural cast is safe.
function makeLocation(over: Partial<Location> = {}): Location {
  return {
    pathname: '/login',
    search: '',
    hash: '',
    state: null,
    key: 'test',
    ...over,
  } as Location;
}

describe('resolvePostLoginPath', () => {
  it('defaults to /execute when nothing was recorded', () => {
    expect(resolvePostLoginPath(makeLocation())).toBe('/execute');
  });

  it('replays the recorded URL (pathname + search + hash)', () => {
    expect(
      resolvePostLoginPath(
        makeLocation({
          state: {
            from: makeLocation({
              pathname: '/execute',
              search: '?signal_id=42',
              hash: '#order-form',
            }),
          },
        }),
      ),
    ).toBe('/execute?signal_id=42#order-form');
  });

  it('handles a from-location with missing search/hash fields', () => {
    expect(
      resolvePostLoginPath(
        makeLocation({
          state: {
            from: {
              pathname: '/signals/7',
              search: '',
              hash: '',
              state: null,
              key: 'x',
            } as Location,
          },
        }),
      ),
    ).toBe('/signals/7');
  });
});
