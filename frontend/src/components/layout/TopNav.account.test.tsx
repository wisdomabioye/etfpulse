/**
 * AccountChip (TopNav) — the connected-account dropdown. Verifies it opens a
 * menu with the full address + Disconnect (rather than linking straight to
 * /execute), and that Disconnect runs the wallet-level disconnect AND the
 * app logout. The wagmi disconnect is reached via a dynamic import of
 * `../../lib/wagmi`, which we mock so no real wagmi/AppKit loads.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mockLogout = vi.fn();
const mockDisconnect = vi.fn().mockResolvedValue(undefined);
const mockUseWalletMe = vi.fn();

vi.mock('../../auth/useAuth', () => ({
  useAuth: () => ({ isAuthed: true, jwt: 'x', login: vi.fn(), logout: mockLogout }),
}));

vi.mock('../../hooks/useExecution', async () => {
  const actual =
    await vi.importActual<typeof import('../../hooks/useExecution')>('../../hooks/useExecution');
  return { ...actual, useWalletMe: () => mockUseWalletMe() };
});

// The dynamic `import('../../lib/wagmi')` in the disconnect handler resolves
// to this mock — no real wagmi bundle is touched in the test.
vi.mock('../../lib/wagmi', () => ({
  disconnectWallet: mockDisconnect,
  isWalletConnectAvailable: true,
  wagmiConfig: {},
}));

import { TopNav } from './TopNav';

const ADDR = '0x' + 'ab'.repeat(20);

function renderNav() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MemoryRouter>
      <QueryClientProvider client={client}>
        <TopNav />
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  mockUseWalletMe.mockReturnValue({
    data: { wallet_address: ADDR, paper_trade: true },
    isLoading: false,
  });
});

afterEach(() => vi.clearAllMocks());

describe('AccountChip', () => {
  it('is a menu button (not a bare /execute link)', () => {
    renderNav();
    expect(screen.getByRole('button', { name: 'Account menu' })).toBeInTheDocument();
  });

  it('opens a dropdown showing the full address + a Disconnect action', () => {
    renderNav();
    // Closed initially.
    expect(screen.queryByRole('button', { name: 'Disconnect' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Account menu' }));
    expect(screen.getByText(ADDR, { exact: false })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Disconnect' })).toBeInTheDocument();
  });

  it('Disconnect runs the wallet disconnect AND the app logout', async () => {
    renderNav();
    fireEvent.click(screen.getByRole('button', { name: 'Account menu' }));
    fireEvent.click(screen.getByRole('button', { name: 'Disconnect' }));
    await waitFor(() => expect(mockDisconnect).toHaveBeenCalledTimes(1));
    expect(mockLogout).toHaveBeenCalledTimes(1);
  });

  it('a Telegram-bound user with no wallet still gets the menu', () => {
    mockUseWalletMe.mockReturnValue({
      data: { wallet_address: null, paper_trade: true },
      isLoading: false,
    });
    renderNav();
    fireEvent.click(screen.getByRole('button', { name: 'Account menu' }));
    expect(screen.getByText(/no wallet bound/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Disconnect' })).toBeInTheDocument();
  });
});
