/**
 * Admin UI parity tests (#186; primitives split #187).
 *
 * Three layers:
 *   1. `parsePositiveId` pure-helper unit tests — edge-case coverage
 *      (15 table cases: positive ids, zero, negative, hex, scientific,
 *      whitespace, non-string).
 *   2. Destructive-confirm-flow regression tests for `UnbindWalletSection`
 *      + `HaltExecutionSection`. Pins the contract that destructive
 *      admin actions REQUIRE a confirm step before firing the mutation,
 *      so a future refactor that drops the gate is caught loudly.
 *   3. Non-destructive flow tests for `PaperTradeSection` (two-button
 *      flip), `ResumeExecutionSection` (scope toggle), `SymbolsRefreshSection`
 *      (single button + 503 path), and `DeliveryTracePanel` (query
 *      submit + table render + empty state).
 *
 * Mock strategy: stub `globalThis.fetch` so the admin mutations don't
 * actually hit the backend; assert on captured request bodies.
 * Components mount inside a fresh `QueryClientProvider` per test so
 * mutation state doesn't bleed across cases.
 *
 * Component-import path (`./admin/sections`) reflects the #187 split —
 * sections live in `pages/admin/sections.tsx` while this test file
 * stays alongside the `Admin` page shell at `pages/Admin.tsx`.
 */

import type { ReactNode } from 'react';

import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { parsePositiveId } from '../lib/parseId';

import {
  DeliveryTracePanel,
  HaltExecutionSection,
  PaperTradeSection,
  ResumeExecutionSection,
  SymbolsRefreshSection,
  UnbindWalletSection,
} from './admin/sections';

const ADMIN_KEY = 'test-admin-key';

// Fresh provider per test — `retry: false` in `defaultOptions.queries`
// prevents the global retry default from masking the first-try error
// path; `gcTime: 0` keeps mutation cache clean across the test suite.
function renderWithClient(ui: ReactNode) {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0 },
      mutations: { retry: false },
    },
  });
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

interface CapturedRequest {
  url: string;
  method: string;
  body: string | undefined;
  headers: Record<string, string>;
}

/** Stub fetch — capture calls + return staged JSON. Mirrors the pattern
 *  in `siwe.test.ts`. */
function stubFetch(
  captured: CapturedRequest[],
  staged: { url: string; status?: number; body: unknown }[],
) {
  return vi.spyOn(globalThis, 'fetch').mockImplementation((url, init) => {
    const u = String(url);
    captured.push({
      url: u,
      method: (init?.method ?? 'GET').toString(),
      body: typeof init?.body === 'string' ? init.body : undefined,
      headers: { ...((init?.headers as Record<string, string>) ?? {}) },
    });
    const match = staged.find((s) => u.endsWith(s.url));
    if (!match) throw new Error(`Unexpected fetch URL: ${u}`);
    return Promise.resolve(
      new Response(JSON.stringify(match.body), {
        status: match.status ?? 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    );
  });
}

// ===========================================================================
// parsePositiveId — pure helper edge cases
// ===========================================================================

describe('parsePositiveId', () => {
  const cases: ReadonlyArray<[string, number | null]> = [
    ['1', 1],
    ['42', 42],
    ['99999999', 99999999],
    [' 7 ', 7], // surrounding whitespace tolerated
    ['', null], // empty
    ['   ', null], // whitespace-only
    ['0', null], // zero is not positive
    ['-1', null], // negative
    ['1.5', null], // non-integer
    ['1e3', null], // scientific notation rejected
    ['abc', null],
    ['12abc', null],
    ['12 34', null], // internal space
    ['0x10', null], // hex rejected
    ['+5', null], // leading + rejected
  ];

  it.each(cases)('parsePositiveId(%j) === %j', (input, expected) => {
    expect(parsePositiveId(input)).toBe(expected);
  });
});

// ===========================================================================
// UnbindWalletSection — destructive confirm flow
// ===========================================================================

describe('UnbindWalletSection', () => {
  let captured: CapturedRequest[];

  beforeEach(() => {
    captured = [];
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('starts with an Unbind button (no confirm visible)', () => {
    renderWithClient(<UnbindWalletSection adminKey={ADMIN_KEY} />);
    expect(screen.getByRole('button', { name: /^unbind…$/i })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^confirm unbind/i })).not.toBeInTheDocument();
  });

  it('disables the Unbind button when user id is missing or invalid', () => {
    renderWithClient(<UnbindWalletSection adminKey={ADMIN_KEY} />);
    const btn = screen.getByRole('button', { name: /^unbind…$/i });
    expect(btn).toBeDisabled();

    const input = screen.getByPlaceholderText('user id');
    fireEvent.change(input, { target: { value: 'abc' } });
    expect(btn).toBeDisabled();

    fireEvent.change(input, { target: { value: '0' } });
    expect(btn).toBeDisabled();

    fireEvent.change(input, { target: { value: '42' } });
    expect(btn).toBeEnabled();
  });

  it('clicking Unbind shows Confirm + Cancel; clicking Cancel reverts', () => {
    renderWithClient(<UnbindWalletSection adminKey={ADMIN_KEY} />);
    fireEvent.change(screen.getByPlaceholderText('user id'), { target: { value: '42' } });
    fireEvent.click(screen.getByRole('button', { name: /^unbind…$/i }));

    // Confirm + Cancel appear, Unbind hidden.
    expect(screen.getByRole('button', { name: /confirm unbind #42/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^cancel$/i })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^unbind…$/i })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /^cancel$/i }));
    expect(screen.getByRole('button', { name: /^unbind…$/i })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /confirm unbind/i })).not.toBeInTheDocument();
  });

  it('does NOT fire the mutation without a confirm click', async () => {
    stubFetch(captured, [
      {
        url: '/api/admin/users/42/unbind-wallet',
        body: { user_id: 42, was_bound: true, previous_wallet_address: '0xabc' },
      },
    ]);
    renderWithClient(<UnbindWalletSection adminKey={ADMIN_KEY} />);
    fireEvent.change(screen.getByPlaceholderText('user id'), { target: { value: '42' } });
    fireEvent.click(screen.getByRole('button', { name: /^unbind…$/i }));

    // Wait briefly to ensure no fetch fires.
    await new Promise((r) => setTimeout(r, 30));
    expect(captured).toHaveLength(0);
  });

  it('confirm click fires the mutation with the admin key header', async () => {
    stubFetch(captured, [
      {
        url: '/api/admin/users/42/unbind-wallet',
        body: { user_id: 42, was_bound: true, previous_wallet_address: '0xabcdef0123456789abcdef0123456789abcdef01' },
      },
    ]);
    renderWithClient(<UnbindWalletSection adminKey={ADMIN_KEY} />);
    fireEvent.change(screen.getByPlaceholderText('user id'), { target: { value: '42' } });
    fireEvent.click(screen.getByRole('button', { name: /^unbind…$/i }));
    fireEvent.click(screen.getByRole('button', { name: /confirm unbind #42/i }));

    await waitFor(() => expect(captured).toHaveLength(1));
    expect(captured[0].method).toBe('POST');
    expect(captured[0].url).toContain('/api/admin/users/42/unbind-wallet');
    expect(captured[0].headers['X-Admin-Key']).toBe(ADMIN_KEY);

    // Success display visible.
    await waitFor(() =>
      expect(screen.getByText(/was_bound = true/i)).toBeInTheDocument(),
    );
  });

  it('shows "no-op" callout when was_bound=false (idempotent unbind)', async () => {
    stubFetch(captured, [
      {
        url: '/api/admin/users/99/unbind-wallet',
        body: { user_id: 99, was_bound: false, previous_wallet_address: null },
      },
    ]);
    renderWithClient(<UnbindWalletSection adminKey={ADMIN_KEY} />);
    fireEvent.change(screen.getByPlaceholderText('user id'), { target: { value: '99' } });
    fireEvent.click(screen.getByRole('button', { name: /^unbind…$/i }));
    fireEvent.click(screen.getByRole('button', { name: /confirm unbind #99/i }));

    await waitFor(() => expect(screen.getByText(/no-op/i)).toBeInTheDocument());
    expect(screen.getByText(/was_bound = false/i)).toBeInTheDocument();
  });

  it('surfaces backend error (e.g., 404 user not found)', async () => {
    stubFetch(captured, [
      {
        url: '/api/admin/users/9999/unbind-wallet',
        status: 404,
        body: { detail: 'user 9999 not found' },
      },
    ]);
    renderWithClient(<UnbindWalletSection adminKey={ADMIN_KEY} />);
    fireEvent.change(screen.getByPlaceholderText('user id'), { target: { value: '9999' } });
    fireEvent.click(screen.getByRole('button', { name: /^unbind…$/i }));
    fireEvent.click(screen.getByRole('button', { name: /confirm unbind #9999/i }));

    await waitFor(() =>
      expect(screen.getByText(/HTTP 404 · user 9999 not found/i)).toBeInTheDocument(),
    );
  });
});

// ===========================================================================
// HaltExecutionSection — destructive confirm + scope toggle
// ===========================================================================

describe('HaltExecutionSection', () => {
  let captured: CapturedRequest[];

  beforeEach(() => {
    captured = [];
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('Halt button starts disabled (no reason)', () => {
    renderWithClient(<HaltExecutionSection adminKey={ADMIN_KEY} />);
    expect(screen.getByRole('button', { name: /^halt…$/i })).toBeDisabled();
  });

  it('enables after reason is typed (default global scope)', () => {
    renderWithClient(<HaltExecutionSection adminKey={ADMIN_KEY} />);
    fireEvent.change(screen.getByPlaceholderText(/reason \(required/i), {
      target: { value: 'manual ops' },
    });
    expect(screen.getByRole('button', { name: /^halt…$/i })).toBeEnabled();
  });

  it('per-user scope requires both reason AND user id', () => {
    renderWithClient(<HaltExecutionSection adminKey={ADMIN_KEY} />);
    fireEvent.click(screen.getByLabelText(/per-user/i));
    const haltBtn = screen.getByRole('button', { name: /^halt…$/i });

    // reason alone → still disabled
    fireEvent.change(screen.getByPlaceholderText(/reason \(required/i), {
      target: { value: 'rogue user' },
    });
    expect(haltBtn).toBeDisabled();

    // add valid id → enabled
    fireEvent.change(screen.getByPlaceholderText('user id'), { target: { value: '7' } });
    expect(haltBtn).toBeEnabled();
  });

  it('does NOT fire the mutation without a confirm click', async () => {
    stubFetch(captured, [
      {
        url: '/api/admin/execution/halt',
        body: { breaker_id: 1, scope: 'global', already_active: false },
      },
    ]);
    renderWithClient(<HaltExecutionSection adminKey={ADMIN_KEY} />);
    fireEvent.change(screen.getByPlaceholderText(/reason \(required/i), {
      target: { value: 'manual ops' },
    });
    fireEvent.click(screen.getByRole('button', { name: /^halt…$/i }));

    await new Promise((r) => setTimeout(r, 30));
    expect(captured).toHaveLength(0);
  });

  it('global confirm sends user_id=null + reason', async () => {
    stubFetch(captured, [
      {
        url: '/api/admin/execution/halt',
        body: {
          breaker_id: 1,
          scope: 'global',
          already_active: false,
          existing_triggered_at: null,
          existing_details: null,
        },
      },
    ]);
    renderWithClient(<HaltExecutionSection adminKey={ADMIN_KEY} />);
    fireEvent.change(screen.getByPlaceholderText(/reason \(required/i), {
      target: { value: 'manual ops' },
    });
    fireEvent.click(screen.getByRole('button', { name: /^halt…$/i }));
    fireEvent.click(screen.getByRole('button', { name: /confirm halt \(global\)/i }));

    await waitFor(() => expect(captured).toHaveLength(1));
    const sent = JSON.parse(captured[0].body!) as { reason: string; user_id: number | null };
    expect(sent.reason).toBe('manual ops');
    expect(sent.user_id).toBeNull();
  });

  it('per-user confirm sends the parsed user_id', async () => {
    stubFetch(captured, [
      {
        url: '/api/admin/execution/halt',
        body: {
          breaker_id: 2,
          scope: 'user',
          already_active: false,
          existing_triggered_at: null,
          existing_details: null,
        },
      },
    ]);
    renderWithClient(<HaltExecutionSection adminKey={ADMIN_KEY} />);
    fireEvent.click(screen.getByLabelText(/per-user/i));
    fireEvent.change(screen.getByPlaceholderText('user id'), { target: { value: '7' } });
    fireEvent.change(screen.getByPlaceholderText(/reason \(required/i), {
      target: { value: 'rogue user' },
    });
    fireEvent.click(screen.getByRole('button', { name: /^halt…$/i }));
    fireEvent.click(screen.getByRole('button', { name: /confirm halt user #7/i }));

    await waitFor(() => expect(captured).toHaveLength(1));
    const sent = JSON.parse(captured[0].body!) as { reason: string; user_id: number | null };
    expect(sent.user_id).toBe(7);
    expect(sent.reason).toBe('rogue user');
  });

  it('shows "already_active" callout with existing details on idempotent re-halt', async () => {
    stubFetch(captured, [
      {
        url: '/api/admin/execution/halt',
        body: {
          breaker_id: 1,
          scope: 'global',
          already_active: true,
          existing_triggered_at: '2026-05-23T12:00:00Z',
          existing_details: { reason: 'manual ops' },
        },
      },
    ]);
    renderWithClient(<HaltExecutionSection adminKey={ADMIN_KEY} />);
    fireEvent.change(screen.getByPlaceholderText(/reason \(required/i), {
      target: { value: 'second attempt' },
    });
    fireEvent.click(screen.getByRole('button', { name: /^halt…$/i }));
    fireEvent.click(screen.getByRole('button', { name: /confirm halt \(global\)/i }));

    await waitFor(() =>
      expect(screen.getByText(/already_active = true/i)).toBeInTheDocument(),
    );
    expect(screen.getByText(/2026-05-23T12:00:00Z/)).toBeInTheDocument();
    expect(screen.getByText(/"reason":"manual ops"/i)).toBeInTheDocument();
  });

  it('Cancel during confirm reverts to Halt button without firing', async () => {
    stubFetch(captured, [
      {
        url: '/api/admin/execution/halt',
        body: { breaker_id: 1, scope: 'global', already_active: false },
      },
    ]);
    renderWithClient(<HaltExecutionSection adminKey={ADMIN_KEY} />);
    fireEvent.change(screen.getByPlaceholderText(/reason \(required/i), {
      target: { value: 'manual ops' },
    });
    fireEvent.click(screen.getByRole('button', { name: /^halt…$/i }));
    expect(screen.getByRole('button', { name: /confirm halt/i })).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /^cancel$/i }));
    expect(screen.getByRole('button', { name: /^halt…$/i })).toBeInTheDocument();

    await new Promise((r) => setTimeout(r, 30));
    expect(captured).toHaveLength(0);
  });
});

// ===========================================================================
// PaperTradeSection — non-destructive, two-button flip
// ===========================================================================

describe('PaperTradeSection', () => {
  let captured: CapturedRequest[];

  beforeEach(() => {
    captured = [];
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('disables both buttons without a valid user id', () => {
    renderWithClient(<PaperTradeSection adminKey={ADMIN_KEY} />);
    expect(screen.getByRole('button', { name: /set true/i })).toBeDisabled();
    expect(screen.getByRole('button', { name: /set false/i })).toBeDisabled();

    fireEvent.change(screen.getByPlaceholderText('user id'), {
      target: { value: '42' },
    });
    expect(screen.getByRole('button', { name: /set true/i })).toBeEnabled();
    expect(screen.getByRole('button', { name: /set false/i })).toBeEnabled();
  });

  it('"Set TRUE" fires with paper_trade=true', async () => {
    stubFetch(captured, [
      {
        url: '/api/admin/users/42/paper-trade',
        body: { user_id: 42, paper_trade: true },
      },
    ]);
    renderWithClient(<PaperTradeSection adminKey={ADMIN_KEY} />);
    fireEvent.change(screen.getByPlaceholderText('user id'), { target: { value: '42' } });
    fireEvent.click(screen.getByRole('button', { name: /set true/i }));

    await waitFor(() => expect(captured).toHaveLength(1));
    const sent = JSON.parse(captured[0].body!) as { paper_trade: boolean };
    expect(sent.paper_trade).toBe(true);
    expect(captured[0].url).toContain('/api/admin/users/42/paper-trade');
    expect(captured[0].headers['X-Admin-Key']).toBe(ADMIN_KEY);

    await waitFor(() =>
      expect(screen.getByText(/paper_trade = true/i)).toBeInTheDocument(),
    );
  });

  it('"Set FALSE" fires with paper_trade=false', async () => {
    stubFetch(captured, [
      {
        url: '/api/admin/users/42/paper-trade',
        body: { user_id: 42, paper_trade: false },
      },
    ]);
    renderWithClient(<PaperTradeSection adminKey={ADMIN_KEY} />);
    fireEvent.change(screen.getByPlaceholderText('user id'), { target: { value: '42' } });
    fireEvent.click(screen.getByRole('button', { name: /set false/i }));

    await waitFor(() => expect(captured).toHaveLength(1));
    const sent = JSON.parse(captured[0].body!) as { paper_trade: boolean };
    expect(sent.paper_trade).toBe(false);
  });
});

// ===========================================================================
// ResumeExecutionSection — non-destructive, scope toggle
// ===========================================================================

describe('ResumeExecutionSection', () => {
  let captured: CapturedRequest[];

  beforeEach(() => {
    captured = [];
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('global Resume button is always enabled (no input required)', () => {
    renderWithClient(<ResumeExecutionSection adminKey={ADMIN_KEY} />);
    expect(screen.getByRole('button', { name: /^resume$/i })).toBeEnabled();
  });

  it('per-user scope disables Resume until a valid id is entered', () => {
    renderWithClient(<ResumeExecutionSection adminKey={ADMIN_KEY} />);
    fireEvent.click(screen.getByLabelText(/per-user/i));
    expect(screen.getByRole('button', { name: /^resume$/i })).toBeDisabled();

    fireEvent.change(screen.getByPlaceholderText('user id'), { target: { value: '7' } });
    expect(screen.getByRole('button', { name: /^resume$/i })).toBeEnabled();
  });

  it('global Resume fires with user_id=null', async () => {
    stubFetch(captured, [
      { url: '/api/admin/execution/resume', body: { rowcount: 1, scope: 'global' } },
    ]);
    renderWithClient(<ResumeExecutionSection adminKey={ADMIN_KEY} />);
    fireEvent.click(screen.getByRole('button', { name: /^resume$/i }));

    await waitFor(() => expect(captured).toHaveLength(1));
    const sent = JSON.parse(captured[0].body!) as { user_id: number | null };
    expect(sent.user_id).toBeNull();
    // Result text is split across spans (`...resolved <span>1</span> breaker`).
    // Match via the document body's flattened textContent — simplest way to
    // assert text that spans node boundaries without false-multiple-match.
    await waitFor(() =>
      expect(document.body.textContent).toContain('resolved 1 breaker'),
    );
  });

  it('shows info tone + "nothing was active" when rowcount=0', async () => {
    stubFetch(captured, [
      { url: '/api/admin/execution/resume', body: { rowcount: 0, scope: 'global' } },
    ]);
    renderWithClient(<ResumeExecutionSection adminKey={ADMIN_KEY} />);
    fireEvent.click(screen.getByRole('button', { name: /^resume$/i }));
    await waitFor(() => expect(screen.getByText(/nothing was active/i)).toBeInTheDocument());
  });
});

// ===========================================================================
// SymbolsRefreshSection — no input, single button
// ===========================================================================

describe('SymbolsRefreshSection', () => {
  let captured: CapturedRequest[];

  beforeEach(() => {
    captured = [];
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('button fires the refresh + renders count tile', async () => {
    stubFetch(captured, [
      {
        url: '/api/admin/sodex/symbols/refresh',
        body: {
          spot_inserted: 3,
          spot_updated: 1,
          perps_inserted: 0,
          perps_updated: 2,
          errors: 0,
        },
      },
    ]);
    renderWithClient(<SymbolsRefreshSection adminKey={ADMIN_KEY} />);
    fireEvent.click(screen.getByRole('button', { name: /^refresh now$/i }));

    await waitFor(() => expect(captured).toHaveLength(1));
    expect(captured[0].method).toBe('POST');
    expect(captured[0].url).toContain('/api/admin/sodex/symbols/refresh');

    await waitFor(() => expect(screen.getByText(/spot inserted/i)).toBeInTheDocument());
  });

  it('shows "up to date" hint when total=0 + no errors', async () => {
    stubFetch(captured, [
      {
        url: '/api/admin/sodex/symbols/refresh',
        body: {
          spot_inserted: 0,
          spot_updated: 0,
          perps_inserted: 0,
          perps_updated: 0,
          errors: 0,
        },
      },
    ]);
    renderWithClient(<SymbolsRefreshSection adminKey={ADMIN_KEY} />);
    fireEvent.click(screen.getByRole('button', { name: /^refresh now$/i }));
    await waitFor(() =>
      expect(screen.getByText(/cache already up to date/i)).toBeInTheDocument(),
    );
  });

  it('surfaces 503 when scheduler is disabled', async () => {
    stubFetch(captured, [
      {
        url: '/api/admin/sodex/symbols/refresh',
        status: 503,
        body: { detail: 'sodex_clients_not_attached' },
      },
    ]);
    renderWithClient(<SymbolsRefreshSection adminKey={ADMIN_KEY} />);
    fireEvent.click(screen.getByRole('button', { name: /^refresh now$/i }));
    await waitFor(() =>
      expect(screen.getByText(/HTTP 503 · sodex_clients_not_attached/i)).toBeInTheDocument(),
    );
  });
});

// ===========================================================================
// DeliveryTracePanel — read-only debug surface
// ===========================================================================

describe('DeliveryTracePanel', () => {
  let captured: CapturedRequest[];

  beforeEach(() => {
    captured = [];
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('Trace button disabled without a valid signal id', () => {
    renderWithClient(<DeliveryTracePanel adminKey={ADMIN_KEY} />);
    expect(screen.getByRole('button', { name: /^trace$/i })).toBeDisabled();

    fireEvent.change(screen.getByPlaceholderText('signal id'), { target: { value: '0' } });
    expect(screen.getByRole('button', { name: /^trace$/i })).toBeDisabled();

    fireEvent.change(screen.getByPlaceholderText('signal id'), { target: { value: '42' } });
    expect(screen.getByRole('button', { name: /^trace$/i })).toBeEnabled();
  });

  it('does NOT fetch until Trace is clicked (keystrokes do not fire)', async () => {
    stubFetch(captured, [
      {
        url: '/api/admin/signals/42/delivery-trace',
        body: {
          signal_id: 42,
          signal_asset: 'BTC',
          signal_type: 'flow_anomaly',
          signal_confidence: 8,
          signal_status: 'delivered',
          delivery_count: 1,
          delivered_count: 1,
          pending_count: 0,
          failed_count: 0,
          skipped_count: 0,
          matched_count: 1,
          recipients: [],
        },
      },
    ]);
    renderWithClient(<DeliveryTracePanel adminKey={ADMIN_KEY} />);
    fireEvent.change(screen.getByPlaceholderText('signal id'), { target: { value: '42' } });
    // Typing should NOT fire the query.
    await new Promise((r) => setTimeout(r, 30));
    expect(captured).toHaveLength(0);

    // Submitting DOES.
    fireEvent.click(screen.getByRole('button', { name: /^trace$/i }));
    await waitFor(() => expect(captured).toHaveLength(1));
  });

  it('renders the recipient table when fetch returns rows', async () => {
    stubFetch(captured, [
      {
        url: '/api/admin/signals/42/delivery-trace',
        body: {
          signal_id: 42,
          signal_asset: 'BTC',
          signal_type: 'flow_anomaly',
          signal_confidence: 8,
          signal_status: 'delivered',
          delivery_count: 2,
          delivered_count: 1,
          pending_count: 0,
          failed_count: 1,
          skipped_count: 0,
          matched_count: 2,
          recipients: [
            {
              kind: 'user',
              target_id: 7,
              target_label: 'alice',
              chat_id: '999',
              target_active: true,
              target_paused: false,
              channel_active: true,
              asset_match: true,
              confidence_match: true,
              matched: true,
              exclude_reason: null,
              delivery_status: 'delivered',
              delivery_attempts: 1,
              delivery_error: null,
            },
            {
              kind: 'group',
              target_id: 12,
              target_label: 'Crypto Crew',
              chat_id: -1001234,
              target_active: true,
              target_paused: false,
              channel_active: null,
              asset_match: true,
              confidence_match: false,
              matched: false,
              exclude_reason: 'confidence below group floor',
              delivery_status: null,
              delivery_attempts: null,
              delivery_error: null,
            },
          ],
        },
      },
    ]);
    renderWithClient(<DeliveryTracePanel adminKey={ADMIN_KEY} />);
    fireEvent.change(screen.getByPlaceholderText('signal id'), { target: { value: '42' } });
    fireEvent.click(screen.getByRole('button', { name: /^trace$/i }));

    // Header context — text is split across spans, use textContent.
    await waitFor(() =>
      expect(document.body.textContent).toContain('Signal #42'),
    );
    // Recipient rows — assert both labels visible.
    expect(screen.getByText(/alice/i)).toBeInTheDocument();
    expect(screen.getByText(/Crypto Crew/i)).toBeInTheDocument();
    // Exclude reason rendered for the un-matched group.
    expect(screen.getByText(/confidence below group floor/i)).toBeInTheDocument();
  });

  it('shows EmptyState when recipients[] is empty', async () => {
    stubFetch(captured, [
      {
        url: '/api/admin/signals/100/delivery-trace',
        body: {
          signal_id: 100,
          signal_asset: 'ETH',
          signal_type: 'magnitude',
          signal_confidence: null,
          signal_status: 'pending',
          delivery_count: 0,
          delivered_count: 0,
          pending_count: 0,
          failed_count: 0,
          skipped_count: 0,
          matched_count: 0,
          recipients: [],
        },
      },
    ]);
    renderWithClient(<DeliveryTracePanel adminKey={ADMIN_KEY} />);
    fireEvent.change(screen.getByPlaceholderText('signal id'), { target: { value: '100' } });
    fireEvent.click(screen.getByRole('button', { name: /^trace$/i }));

    await waitFor(() =>
      expect(screen.getByText(/no recipients evaluated/i)).toBeInTheDocument(),
    );
  });
});
