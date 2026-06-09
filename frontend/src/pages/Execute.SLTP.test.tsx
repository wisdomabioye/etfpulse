/**
 * PR P1-fix.G2 / G3 — render-level tests for the SL/TP/reduce-only
 * inputs + Close-Position button.
 *
 * Companion to `Execute.Prefill.test.tsx` (SIG2X prefill) and
 * `orderChain.test.ts` (pure chain logic). This file pins the form's
 * conditional rendering across venue switches + the close-button
 * interaction, which are the two FE behaviors my P1 review flagged
 * as untested.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mockUseAuth = vi.fn();
const mockUseSignal = vi.fn();
const mockUseWalletMe = vi.fn();
const mockUsePositions = vi.fn();
const mockClosePosition = vi.fn();
const mockSubmitNew = vi.fn();
const mockPrepare = vi.fn();

vi.mock('../auth/useAuth', () => ({
  useAuth: () => mockUseAuth(),
}));

vi.mock('../api/queries', () => ({
  useSignal: () => mockUseSignal(),
}));

vi.mock('wagmi', () => ({
  useAccount: () => ({ address: '0x' + 'ab'.repeat(20), isConnected: true }),
  useSignMessage: () => ({ signMessageAsync: vi.fn() }),
  useSignTypedData: () => ({
    signTypedDataAsync: vi.fn().mockResolvedValue('0x' + '0'.repeat(130) + '1b'),
  }),
}));

vi.mock('@reown/appkit/react', () => ({
  useAppKit: () => ({ open: vi.fn() }),
}));

vi.mock('../lib/wagmi', () => ({
  isWalletConnectAvailable: true,
}));

vi.mock('../hooks/useExecution', async () => {
  const actual =
    await vi.importActual<typeof import('../hooks/useExecution')>(
      '../hooks/useExecution',
    );
  return {
    ...actual,
    useWalletMe: () => mockUseWalletMe(),
    useOrders: () => ({ data: { items: [] }, isLoading: false, isError: false }),
    usePositions: () => mockUsePositions(),
    useSymbols: () => ({
      data: {
        items: [
          { venue: 'sodex_spot', asset: 'BTC', symbol: 'BTCUSDT' },
          { venue: 'sodex_perps', asset: 'BTC', symbol: 'BTCUSDT' },
        ],
      },
      isLoading: false,
      isError: false,
    }),
    usePrepareNew: () => ({ mutate: vi.fn(), mutateAsync: mockPrepare, isPending: false }),
    useSubmitNew: () => ({
      mutate: vi.fn(),
      mutateAsync: mockSubmitNew,
      isPending: false,
    }),
    usePrepareCancel: () => ({ mutateAsync: vi.fn(), isPending: false }),
    useSubmitCancel: () => ({ mutateAsync: vi.fn(), isPending: false }),
    useClosePosition: () => ({
      mutate: vi.fn(),
      mutateAsync: mockClosePosition,
      isPending: false,
    }),
    useRequestLive: () => ({ mutate: vi.fn(), isPending: false, data: undefined }),
    useSodexBootstrap: () => ({ data: undefined, isLoading: false, isError: false }),
    useSetApiKey: () => ({
      mutate: vi.fn(),
      isPending: false,
      isError: false,
      error: null,
    }),
  };
});

import { Execute } from './Execute';

const FULLY_BOUND_ME = {
  user_id: 1,
  wallet_address: '0x' + 'ab'.repeat(20),
  sodex_account_id: 42,
  paper_trade: true,
  sodex_spot_api_key_name: 'prod',
  sodex_perps_api_key_name: 'prod',
};

const OPEN_PERPS_POSITION = {
  id: 7,
  user_id: 1,
  venue: 'sodex_perps' as const,
  asset: 'BTC',
  side: 'long' as const,
  size: '0.01',
  entry_price: '65000',
  leverage: '3',
  status: 'open' as const,
};

function renderAt(url: string) {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0 },
      mutations: { retry: false },
    },
  });
  return render(
    <MemoryRouter initialEntries={[url]}>
      <QueryClientProvider client={client}>
        <Routes>
          <Route path="/execute" element={<Execute />} />
        </Routes>
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  mockUseAuth.mockReturnValue({
    jwt: 'fake',
    isAuthed: true,
    login: vi.fn(),
    logout: vi.fn(),
  });
  mockUseWalletMe.mockReturnValue({
    data: FULLY_BOUND_ME,
    isLoading: false,
    isError: false,
  });
  mockUseSignal.mockReturnValue({
    data: undefined,
    isLoading: false,
    isError: false,
    error: null,
  });
  mockUsePositions.mockReturnValue({
    data: { items: [] },
    isLoading: false,
    isError: false,
  });
  mockClosePosition.mockReset();
  mockSubmitNew.mockReset();
  mockPrepare.mockReset();
});

afterEach(() => {
  vi.clearAllMocks();
});

// ---------------------------------------------------------------------------
// G3 — perps-only conditional rendering of SL / TP / reduce-only
// ---------------------------------------------------------------------------

describe('OrderForm — perps-only SL/TP/reduce-only inputs', () => {
  it('hides SL / TP / reduce-only inputs on spot venue (default for BUY)', () => {
    renderAt('/execute');
    // With both api keys present, the form defaults to spot (first
    // venue offered). Stop / Take-profit / Reduce-only inputs are
    // perps-only — they must NOT render on the spot view.
    expect(screen.queryByLabelText(/stop loss price/i)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/take profit price/i)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/reduce only/i)).not.toBeInTheDocument();
  });

  it('shows SL / TP / reduce-only inputs after switching venue to perps', () => {
    renderAt('/execute');
    // Venue is a segmented toggle now — tap "Perps" to switch.
    fireEvent.click(screen.getByRole('button', { name: 'Perps' }));
    expect(screen.getByLabelText(/stop loss price/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/take profit price/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/reduce only/i)).toBeInTheDocument();
  });

  it('side toggle is Buy/Sell on spot and Long/Short on perps', () => {
    renderAt('/execute');
    // Default venue is spot (first key offered) — spot has no shorting,
    // so the side toggle reads Buy / Sell, not Long / Short.
    expect(screen.getByRole('button', { name: 'Buy' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Sell' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Long' })).not.toBeInTheDocument();
    // Switch to perps → directional Long / Short.
    fireEvent.click(screen.getByRole('button', { name: 'Perps' }));
    expect(screen.getByRole('button', { name: 'Long' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Short' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Buy' })).not.toBeInTheDocument();
  });

  it('DBLSUB-1: two form submits start the entry chain exactly once', () => {
    // Keep the first prepare in-flight (never resolves) so the chain
    // stays running while we fire the second submit.
    mockPrepare.mockReturnValue(new Promise(() => {}));
    renderAt('/execute');
    fireEvent.change(screen.getByLabelText(/size/i), { target: { value: '0.01' } });
    fireEvent.change(screen.getByLabelText(/limit price/i), { target: { value: '65000' } });
    // Submit the FORM directly, twice. This bypasses the disabled-button
    // path (jsdom flushes the disabled re-render synchronously, which
    // would otherwise mask the race), so ONLY the synchronous in-flight
    // ref guard can prevent the second chain from starting.
    const form = screen.getByRole('button', { name: /place order/i }).closest('form')!;
    fireEvent.submit(form);
    fireEvent.submit(form);
    expect(mockPrepare).toHaveBeenCalledTimes(1);
  });
});

// ---------------------------------------------------------------------------
// G2 — Close-Position button click flow
// ---------------------------------------------------------------------------

describe('PositionRow — Close button', () => {
  it('renders a Close button per open position', () => {
    mockUsePositions.mockReturnValue({
      data: { items: [OPEN_PERPS_POSITION] },
      isLoading: false,
      isError: false,
    });
    renderAt('/execute');
    expect(screen.getByLabelText(/close BTC long position/i)).toBeInTheDocument();
  });

  it('modal-cancelled close does NOT call closePosition', () => {
    mockUsePositions.mockReturnValue({
      data: { items: [OPEN_PERPS_POSITION] },
      isLoading: false,
      isError: false,
    });
    renderAt('/execute');
    // Tapping the row Close button opens the confirm modal but signs
    // nothing; dismissing it must NOT start the close chain.
    fireEvent.click(screen.getByLabelText(/close BTC long position/i));
    fireEvent.click(screen.getByRole('button', { name: /^cancel$/i }));
    expect(mockClosePosition).not.toHaveBeenCalled();
  });

  it('modal-confirmed close invokes closePosition with the position id', async () => {
    mockUsePositions.mockReturnValue({
      data: { items: [OPEN_PERPS_POSITION] },
      isLoading: false,
      isError: false,
    });
    mockClosePosition.mockResolvedValue({
      order_id: 555,
      client_order_id: 'co-555',
      nonce: 1,
      typed_data: {
        types: {},
        primaryType: 'ExchangeAction',
        domain: { name: 'futures', version: '1', chainId: 138565, verifyingContract: '0x0' },
        message: {},
      },
    });
    mockSubmitNew.mockResolvedValue({ order_id: 555, status: 'filled' });
    renderAt('/execute');
    fireEvent.click(screen.getByLabelText(/close BTC long position/i));
    // Confirm in the modal — this is what actually starts the chain.
    fireEvent.click(screen.getByRole('button', { name: /close · sign 1×/i }));
    // microtask flush
    await Promise.resolve();
    await Promise.resolve();
    expect(mockClosePosition).toHaveBeenCalledWith(OPEN_PERPS_POSITION.id);
  });
});

// ---------------------------------------------------------------------------
// SoDEX-setup gating — no empty trade panels before the wallet is provisioned
// ---------------------------------------------------------------------------

describe('Execute — SoDEX setup gating', () => {
  it('hides the order form + positions/orders panels until the wallet is set up', () => {
    mockUseWalletMe.mockReturnValue({
      data: {
        ...FULLY_BOUND_ME,
        sodex_account_id: null,
        sodex_spot_api_key_name: null,
        sodex_perps_api_key_name: null,
      },
      isLoading: false,
      isError: false,
    });
    renderAt('/execute');
    // The trade grid must NOT render — no empty "Open positions"/order form.
    expect(screen.queryByText('Open positions')).not.toBeInTheDocument();
    expect(screen.queryByText('New order')).not.toBeInTheDocument();
    // The compact setup notice is shown instead (bootstrap mock returns
    // undefined → the "unavailable" guidance shell).
    expect(screen.getByText(/sodex bootstrap unavailable/i)).toBeInTheDocument();
  });
});
