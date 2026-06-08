/**
 * ApiKeySection tests (SDXB.5 / task #215).
 *
 * Pins every render branch of the bootstrap-aware section:
 *   - All venues bound → returns null (nothing to render).
 *   - Bootstrap loading → loading shell visible.
 *   - Bootstrap error → manual-fallback shell visible.
 *   - Bootstrap data: account_id null → "no SoDEX account" shell.
 *   - Single key per missing venue → auto-bind fires EXACTLY once
 *     via the useRef guard.
 *   - Multi-key per missing venue → dropdown + manual bind button.
 *   - Zero keys per missing venue → "register a key" guidance.
 *   - Mixed: 1 spot key + 2 perps keys → auto-bind spot, dropdown perps.
 *
 * Mocks `useSodexBootstrap` + `useSetApiKey` from
 * `../../hooks/useExecution`. The page-level invalidation of
 * `KEY_WALLET_ME` is exercised by `useSetApiKey` itself (existing
 * tests in hooks suite); here we only assert the mutation call shape.
 */

import type { ReactNode } from 'react';

import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type {
  APIKeyOut,
  SodexBootstrapResponse,
  WalletMeResponse,
} from '../../api/execution';

const mockBootstrap = vi.fn();
const mockSetKey = vi.fn();

vi.mock('../../hooks/useExecution', () => ({
  useSodexBootstrap: () => mockBootstrap(),
  useSetApiKey: () => mockSetKey(),
}));

import { ApiKeySection } from './ApiKeySection';

/* ─────────────────────────── helpers ─────────────────────────── */

function withClient(ui: ReactNode) {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0 },
      mutations: { retry: false },
    },
  });
  return <QueryClientProvider client={client}>{ui}</QueryClientProvider>;
}

function makeKey(name: string): APIKeyOut {
  return { name, public_key: '0x' + 'ab'.repeat(33), expires_at: 0 };
}

function makeMe(over: Partial<WalletMeResponse> = {}): WalletMeResponse {
  return {
    user_id: 1,
    wallet_address: '0x' + 'cd'.repeat(20),
    sodex_account_id: null,
    paper_trade: true,
    sodex_spot_api_key_name: null,
    sodex_perps_api_key_name: null,
    ...over,
  };
}

function makeBootstrap(over: Partial<SodexBootstrapResponse> = {}): SodexBootstrapResponse {
  return {
    account_id: 42,
    spot_keys: [],
    perps_keys: [],
    ...over,
  };
}

interface MutationStub {
  mutate: ReturnType<typeof vi.fn>;
  isPending: boolean;
  isError: boolean;
  error: Error | null;
}

function setKeyStub(over: Partial<MutationStub> = {}): MutationStub {
  return {
    mutate: vi.fn(),
    isPending: false,
    isError: false,
    error: null,
    ...over,
  };
}

interface QueryStub<T> {
  data: T | undefined;
  isLoading: boolean;
  isError: boolean;
  error: Error | null;
}

function bootstrapStub<T>(over: Partial<QueryStub<T>> = {}): QueryStub<T> {
  return {
    data: undefined,
    isLoading: false,
    isError: false,
    error: null,
    ...over,
  };
}

beforeEach(() => {
  mockBootstrap.mockReturnValue(bootstrapStub<SodexBootstrapResponse>({ data: makeBootstrap() }));
  mockSetKey.mockReturnValue(setKeyStub());
});

afterEach(() => {
  vi.clearAllMocks();
});

/* ─────────────────────────── render states ─────────────────────────── */

describe('ApiKeySection — gate', () => {
  it('renders null when both venues are already bound', () => {
    const { container } = render(
      withClient(
        <ApiKeySection
          me={makeMe({
            sodex_spot_api_key_name: 'prod',
            sodex_perps_api_key_name: 'prod',
          })}
        />,
      ),
    );
    expect(container.firstChild).toBeNull();
  });

  it('renders null when wallet is not bound (parent owns that messaging)', () => {
    // Regression: a wallet-less Telegram-WebApp user would previously
    // see the "SoDEX bootstrap unavailable" shell, which is confusing
    // when the actual problem is "you haven't bound a wallet yet" —
    // a state the Execute page already surfaces via WalletMissingNotice.
    const { container } = render(
      withClient(<ApiKeySection me={makeMe({ wallet_address: null })} />),
    );
    expect(container.firstChild).toBeNull();
    // Bootstrap must NOT be called when there's no wallet — would
    // 403 on the backend and clutter the network panel.
    expect(mockBootstrap).not.toHaveBeenCalled();
  });
});

describe('ApiKeySection — bootstrap states', () => {
  it('renders the loading shell while bootstrap is in flight', () => {
    mockBootstrap.mockReturnValue(bootstrapStub<SodexBootstrapResponse>({ isLoading: true }));
    render(withClient(<ApiKeySection me={makeMe()} />));
    expect(screen.getByText(/reading SoDEX account/i)).toBeInTheDocument();
  });

  it('falls back to the manual-fallback shell on bootstrap error', () => {
    mockBootstrap.mockReturnValue(
      bootstrapStub<SodexBootstrapResponse>({
        isError: true,
        error: new Error('503 SoDEX bootstrap unavailable'),
      }),
    );
    render(withClient(<ApiKeySection me={makeMe()} />));
    expect(screen.getByText(/SoDEX bootstrap unavailable/i)).toBeInTheDocument();
  });

  it('shows the "no SoDEX account" shell when account_id is null', () => {
    mockBootstrap.mockReturnValue(
      bootstrapStub<SodexBootstrapResponse>({ data: makeBootstrap({ account_id: null }) }),
    );
    render(withClient(<ApiKeySection me={makeMe()} />));
    expect(screen.getByText(/no SoDEX account for this wallet/i)).toBeInTheDocument();
  });
});

describe('ApiKeySection — per-venue', () => {
  it('shows register-a-key guidance for a venue with zero keys', () => {
    mockBootstrap.mockReturnValue(
      bootstrapStub<SodexBootstrapResponse>({
        data: makeBootstrap({ spot_keys: [], perps_keys: [] }),
      }),
    );
    render(withClient(<ApiKeySection me={makeMe()} />));
    expect(screen.getByText(/No Spot API key registered/i)).toBeInTheDocument();
    expect(screen.getByText(/No Perps API key registered/i)).toBeInTheDocument();
  });

  it('auto-binds when exactly one key is registered per missing venue', async () => {
    const setKey = setKeyStub();
    mockSetKey.mockReturnValue(setKey);
    mockBootstrap.mockReturnValue(
      bootstrapStub<SodexBootstrapResponse>({
        data: makeBootstrap({
          account_id: 99,
          spot_keys: [makeKey('prod-spot')],
          perps_keys: [makeKey('prod-perps')],
        }),
      }),
    );
    render(withClient(<ApiKeySection me={makeMe()} />));
    await waitFor(() => {
      expect(setKey.mutate).toHaveBeenCalledTimes(2);
    });
    expect(setKey.mutate).toHaveBeenCalledWith({
      venue: 'sodex_spot',
      api_key_name: 'prod-spot',
      sodex_account_id: 99,
    });
    expect(setKey.mutate).toHaveBeenCalledWith({
      venue: 'sodex_perps',
      api_key_name: 'prod-perps',
      sodex_account_id: 99,
    });
  });

  it('renders a dropdown when multiple keys are registered', async () => {
    const setKey = setKeyStub();
    mockSetKey.mockReturnValue(setKey);
    mockBootstrap.mockReturnValue(
      bootstrapStub<SodexBootstrapResponse>({
        data: makeBootstrap({
          account_id: 7,
          spot_keys: [makeKey('prod'), makeKey('dev'), makeKey('temp')],
          perps_keys: [makeKey('prod')],
        }),
      }),
    );
    render(
      withClient(
        <ApiKeySection
          me={makeMe({
            sodex_spot_api_key_name: null,
            sodex_perps_api_key_name: 'prod',
          })}
        />,
      ),
    );
    const select = screen.getByLabelText(/Spot API key/i);
    expect(select).toBeInTheDocument();
    const options = within(select as HTMLSelectElement).getAllByRole('option');
    expect(options.map((o) => o.textContent)).toEqual(['prod', 'dev', 'temp']);

    // Pick the second key and bind.
    fireEvent.change(select, { target: { value: 'dev' } });
    fireEvent.click(screen.getByRole('button', { name: /bind key/i }));
    expect(setKey.mutate).toHaveBeenCalledWith({
      venue: 'sodex_spot',
      api_key_name: 'dev',
      sodex_account_id: 7,
    });
  });

  it('mixed: 1 spot key auto-binds while 2 perps keys render a dropdown', async () => {
    const setKey = setKeyStub();
    mockSetKey.mockReturnValue(setKey);
    mockBootstrap.mockReturnValue(
      bootstrapStub<SodexBootstrapResponse>({
        data: makeBootstrap({
          account_id: 7,
          spot_keys: [makeKey('only-spot')],
          perps_keys: [makeKey('a'), makeKey('b')],
        }),
      }),
    );
    render(withClient(<ApiKeySection me={makeMe()} />));

    // Spot auto-bind fires exactly once on mount.
    await waitFor(() => {
      expect(setKey.mutate).toHaveBeenCalledWith({
        venue: 'sodex_spot',
        api_key_name: 'only-spot',
        sodex_account_id: 7,
      });
    });

    // Perps shows its dropdown — user hasn't picked yet so mutate
    // was called exactly once (the spot auto-bind), not twice.
    expect(setKey.mutate).toHaveBeenCalledTimes(1);
    expect(screen.getByLabelText(/Perps API key/i)).toBeInTheDocument();
  });

  it('surfaces a bind error from the mutation in the panel body', () => {
    const setKey = setKeyStub({
      isError: true,
      error: new Error('account not found'),
    });
    mockSetKey.mockReturnValue(setKey);
    mockBootstrap.mockReturnValue(
      bootstrapStub<SodexBootstrapResponse>({
        data: makeBootstrap({
          spot_keys: [makeKey('one')],
        }),
      }),
    );
    render(
      withClient(
        <ApiKeySection
          me={makeMe({ sodex_perps_api_key_name: 'already-bound' })}
        />,
      ),
    );
    expect(screen.getByText(/account not found/i)).toBeInTheDocument();
  });
});
