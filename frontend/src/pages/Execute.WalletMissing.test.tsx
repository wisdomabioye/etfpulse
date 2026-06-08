/**
 * Regression test for the `<WalletMissingNotice>` AppKit-throw guard
 * (#78.4).
 *
 * The bug we're catching:
 *   `<WalletMissingWithWallet>` calls `useAccount`/`useAppKit`/
 *   `useSignMessage` at the top of its render. These hooks throw if
 *   WagmiProvider isn't in the React tree OR `createAppKit` was never
 *   called. The `<WalletMissingNotice>` component gates on
 *   `isWalletConnectAvailable` and routes to `<WalletMissingUnavailable>`
 *   (no hooks) when AppKit isn't initialised.
 *
 *   If a refactor removes the `if (!isWalletConnectAvailable)` gate,
 *   visiting `/execute` on a deploy without `VITE_WALLETCONNECT_PROJECT_ID`
 *   crashes the page for every Telegram-bound user (the original D.5
 *   bug, which we already fixed once).
 *
 * The test:
 *   Render `<WalletMissingNotice>` WITHOUT WagmiProvider in the tree.
 *   In the test environment, `VITE_WALLETCONNECT_PROJECT_ID` is unset,
 *   so `isWalletConnectAvailable = false` and the gate routes to
 *   `<WalletMissingUnavailable>`. The render succeeds; the user sees
 *   the "Wallet Connect isn't configured" message.
 *
 *   If the gate is removed (regression), `<WalletMissingNotice>` would
 *   directly try to call AppKit hooks, throwing inside the test render
 *   call — the test fails LOUDLY rather than the regression slipping
 *   into a deploy.
 *
 *   Mirror this shape for the `<Login>` page's wallet gate when its
 *   regression test lands.
 */

import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

// Force-mock the gate's flag so this test doesn't depend on whether
// the running shell happens to have `VITE_WALLETCONNECT_PROJECT_ID`
// set in .env (which the project ID now is, for live dev testing).
// Without this, AppKit initialises at import time inside jsdom and
// `<WalletMissingNotice>` routes through the AppKit-hook path that
// requires WagmiProvider — exactly what the test is trying to AVOID.
vi.mock('../lib/wagmi', () => ({
  isWalletConnectAvailable: false,
}));

import { WalletMissingNotice, WalletMissingUnavailable } from './Execute';

describe('WalletMissingNotice — AppKit-throw guard', () => {
  it('renders without WagmiProvider when VITE_WALLETCONNECT_PROJECT_ID is unset', () => {
    // No providers, no AppKit init — the gate must route to
    // <WalletMissingUnavailable>, which doesn't call any wagmi/AppKit
    // hooks. If a regression removes the gate, this render throws and
    // the test fails.
    render(<WalletMissingNotice />);
    expect(screen.getByRole('heading', { name: /wallet not bound/i })).toBeInTheDocument();
  });

  it('shows the operator-facing misconfiguration message', () => {
    render(<WalletMissingNotice />);
    // The exact phrase "isn't configured" is the operator-actionable
    // signal — pin it so a copy edit doesn't silently lose the cue.
    expect(screen.getByText(/wallet connect isn'?t configured/i)).toBeInTheDocument();
  });

  it('mentions the missing env var by name', () => {
    render(<WalletMissingNotice />);
    // Operators copy-paste this name into their deploy env. Pinning
    // prevents a typo from leaking into the user-facing message.
    expect(screen.getByText(/VITE_WALLETCONNECT_PROJECT_ID/i)).toBeInTheDocument();
  });
});

describe('WalletMissingUnavailable — pure render', () => {
  it('renders the same content directly (independent of the gate)', () => {
    // Render the leaf component directly to confirm it doesn't depend
    // on any context — a useful sanity check before the next refactor
    // that touches the split.
    render(<WalletMissingUnavailable />);
    expect(screen.getByText(/wallet connect isn'?t configured/i)).toBeInTheDocument();
  });

  it('uses an amber warning visual cue (not destructive red)', () => {
    // Misconfig is a deploy-config problem, not a user error. Amber
    // signals "operator must fix"; red would imply user fault.
    const { container } = render(<WalletMissingUnavailable />);
    const section = container.querySelector('section');
    expect(section).toBeTruthy();
    expect(section?.className).toMatch(/amber/);
  });
});
