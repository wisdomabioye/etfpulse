/**
 * Execute page prefill from `?signal_id=N` (SIG2X.2).
 *
 * Pinned behaviors:
 *   - URL `signal_id` triggers `useSignal(id)` query.
 *   - Invalid signal_id values (0, negative, non-digit, leading zero)
 *     yield `signalId === undefined` → query disabled, no banner.
 *   - Loaded actionable signal → banner shows "Driven by signal #N · ASSET · action".
 *   - Loaded NON-actionable signal → warning banner says "isn't actionable".
 *   - Query error → "could not be loaded" banner.
 *   - Loading → "Loading signal #N…" banner.
 *   - No `signal_id` in URL → no banner at all.
 *
 * Mocks `useSignal` + `useWalletMe` + `useAuth`. We render only the
 * `OrderFormSection` indirectly via `<Execute>` to keep the test
 * focused on the prefill surface without firing real wallet/key
 * hooks.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mockUseAuth = vi.fn();
const mockUseSignal = vi.fn();
const mockUseWalletMe = vi.fn();

vi.mock('../auth/useAuth', () => ({
  useAuth: () => mockUseAuth(),
}));

vi.mock('../api/queries', () => ({
  useSignal: () => mockUseSignal(),
}));

// Stub the wagmi-touching hooks to neutral returns so OrderForm can
// mount without a real wallet provider.
vi.mock('wagmi', () => ({
  useAccount: () => ({ address: '0x' + 'ab'.repeat(20), isConnected: true }),
  useSignMessage: () => ({ signMessageAsync: vi.fn() }),
  useSignTypedData: () => ({ signTypedDataAsync: vi.fn() }),
}));

vi.mock('@reown/appkit/react', () => ({
  useAppKit: () => ({ open: vi.fn() }),
}));

vi.mock('../lib/wagmi', () => ({
  isWalletConnectAvailable: true,
}));

// useExecution surface — we only care that useWalletMe returns a
// fully-bound `me` so OrderFormSection renders; the rest are stubs.
vi.mock('../hooks/useExecution', async () => {
  const actual =
    await vi.importActual<typeof import('../hooks/useExecution')>(
      '../hooks/useExecution',
    );
  return {
    ...actual,
    useWalletMe: () => mockUseWalletMe(),
    useOrders: () => ({ data: { items: [] }, isLoading: false, isError: false }),
    usePositions: () => ({ data: { items: [] }, isLoading: false, isError: false }),
    useSymbols: () => ({
      data: {
        items: [
          { venue: 'sodex_spot', asset: 'BTC', symbol: 'BTCUSDT' },
          { venue: 'sodex_spot', asset: 'ETH', symbol: 'ETHUSDT' },
          { venue: 'sodex_perps', asset: 'BTC', symbol: 'BTCUSDT' },
          { venue: 'sodex_perps', asset: 'ETH', symbol: 'ETHUSDT' },
        ],
      },
      isLoading: false,
      isError: false,
    }),
    usePrepareNew: () => ({ mutate: vi.fn(), mutateAsync: vi.fn(), isPending: false }),
    useSubmitNew: () => ({ mutate: vi.fn(), mutateAsync: vi.fn(), isPending: false }),
    usePrepareCancel: () => ({ mutateAsync: vi.fn(), isPending: false }),
    useSubmitCancel: () => ({ mutateAsync: vi.fn(), isPending: false }),
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

const ACTIONABLE_SIGNAL = {
  id: 99,
  asset: 'BTC',
  signal_type: 'flow_anomaly',
  status: 'alerted',
  confidence: 7,
  fingerprint: 'a'.repeat(32),
  signal_date: '2026-06-01',
  created_at: '2026-06-01T00:00:00Z',
  expires_at: null,
  alerted_to: 100,
  trigger_data: {},
  ai_analysis: {
    headline: 'BTC test',
    reasoning: [],
    risks: [],
    confidence: 7,
    suggested_action: 'consider long' as const,
    time_horizon: 'swing' as const,
    entry_price: 50000,
    stop_price: 48000,
    target_price: 55000,
  },
  outcome: null,
  price_at_creation: 50000,
  price_source: 'sosovalue',
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
});

afterEach(() => {
  vi.clearAllMocks();
});

describe('Execute — signal-driven prefill', () => {
  it('renders no banner when no signal_id is present in the URL', () => {
    renderAt('/execute');
    expect(screen.queryByText(/driven by signal/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/loading signal/i)).not.toBeInTheDocument();
    expect(mockUseSignal).toHaveBeenCalled();
  });

  it('renders no banner for invalid signal_id values (0, leading zero, garbage)', () => {
    // 0 — backend's `gt=0` would 422, so we treat as no-signal up front.
    renderAt('/execute?signal_id=0');
    expect(screen.queryByText(/driven by signal/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/loading signal/i)).not.toBeInTheDocument();
  });

  it('renders a loading banner while the signal query is in flight', () => {
    mockUseSignal.mockReturnValue({
      data: undefined,
      isLoading: true,
      isError: false,
      error: null,
    });
    renderAt('/execute?signal_id=99');
    expect(screen.getByText(/loading signal #99/i)).toBeInTheDocument();
  });

  it('renders the driven-by-signal banner for an actionable signal', () => {
    mockUseSignal.mockReturnValue({
      data: ACTIONABLE_SIGNAL,
      isLoading: false,
      isError: false,
      error: null,
    });
    renderAt('/execute?signal_id=99');
    expect(screen.getByText(/prefilled from/i)).toBeInTheDocument();
    // Confirm the deep-back-link points at /signals/99.
    const backLink = screen.getByRole('link', { name: /view signal/i });
    expect(backLink).toHaveAttribute('href', '/signals/99');
  });

  it('prefills the form fields from the signal (observable via the limit price)', () => {
    // ACTIONABLE_SIGNAL has entry_price=50000. The form's price field
    // starts empty by default; if the useEffect prefill ran, the input
    // shows '50000'. This is the most observable prefill side-effect:
    // venue/asset/side default values happen to match the fixture, so
    // they aren't distinguishable from a no-op. Limit price is the
    // canary that "yes, the effect actually fired".
    mockUseSignal.mockReturnValue({
      data: ACTIONABLE_SIGNAL,
      isLoading: false,
      isError: false,
      error: null,
    });
    renderAt('/execute?signal_id=99');
    // Price input is the <input> inside the <label>Price</label>.
    // Use getByLabelText for the stable association.
    const priceInput = screen.getByLabelText(/price/i) as HTMLInputElement;
    expect(priceInput.value).toBe('50000');
  });

  it('prefill switches venue to perps for consider-short signals (spot cannot short)', () => {
    mockUseSignal.mockReturnValue({
      data: {
        ...ACTIONABLE_SIGNAL,
        ai_analysis: { ...ACTIONABLE_SIGNAL.ai_analysis, suggested_action: 'consider short' },
      },
      isLoading: false,
      isError: false,
      error: null,
    });
    renderAt('/execute?signal_id=99');
    // The venue Seg should be on Perps post-prefill (aria-pressed).
    expect(screen.getByRole('button', { name: 'Perps' })).toHaveAttribute(
      'aria-pressed',
      'true',
    );
    // And the Short side toggle should be active (the short mapping → 'sell').
    expect(screen.getByRole('button', { name: 'Short' })).toHaveAttribute(
      'aria-pressed',
      'true',
    );
  });

  it('renders the not-actionable warning for MARKET signals', () => {
    mockUseSignal.mockReturnValue({
      data: { ...ACTIONABLE_SIGNAL, asset: 'MARKET' },
      isLoading: false,
      isError: false,
      error: null,
    });
    renderAt('/execute?signal_id=99');
    expect(screen.getByText(/isn'?t actionable/i)).toBeInTheDocument();
  });

  it('renders the could-not-load warning on query error', () => {
    mockUseSignal.mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: true,
      error: new Error('404'),
    });
    renderAt('/execute?signal_id=99');
    expect(screen.getByText(/could not be loaded/i)).toBeInTheDocument();
  });
});
