/**
 * SignalDetail "⚡ Execute this signal" CTA — render-level regression
 * tests (SIG2X.1).
 *
 * Pinned behaviors:
 *   - Renders the CTA for BTC/ETH + actionable direction.
 *   - Hides for MARKET asset.
 *   - Hides for `'wait'` direction.
 *   - Hides for AI-failed signals (`ai_analysis: null`).
 *   - Hides while the signal query is loading (no `data` yet).
 *   - CTA href is exactly `/execute?signal_id={id}`.
 *
 * The pure-fn gate is tested in `lib/signalExecute.test.ts`; this
 * file pins the JSX integration (the gate IS imported by the page).
 */

import { render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mockUseSignal = vi.fn();

vi.mock('../api/queries', () => ({
  useSignal: () => mockUseSignal(),
}));

import { SignalDetail } from './SignalDetail';

function renderAt(id: number, signalData: unknown, opts: { isLoading?: boolean; isError?: boolean } = {}) {
  mockUseSignal.mockReturnValue({
    data: signalData,
    isLoading: opts.isLoading ?? false,
    isError: opts.isError ?? false,
    error: opts.isError ? new Error('boom') : null,
    refetch: vi.fn(),
  });
  return render(
    <MemoryRouter initialEntries={[`/signals/${id}`]}>
      <Routes>
        <Route path="/signals/:id" element={<SignalDetail />} />
      </Routes>
    </MemoryRouter>,
  );
}

const SAMPLE_ANALYSIS = {
  headline: 'Test signal',
  reasoning: [],
  risks: [],
  confidence: 7,
  suggested_action: 'consider long' as const,
  time_horizon: 'swing' as const,
  entry_price: 50000,
  stop_price: 48000,
  target_price: 55000,
};

function mkSignal(over: Partial<Record<string, unknown>> = {}) {
  return {
    id: 42,
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
    ai_analysis: SAMPLE_ANALYSIS,
    outcome: null,
    price_at_creation: 50000,
    price_source: 'sosovalue',
    delivery_status_counts: { pending: 0, delivered: 100, failed: 0, skipped: 0 },
    confirmation_score: null,
    factor_votes: null,
    ai_prompt_version: 'v3',
    ...over,
  };
}

beforeEach(() => {
  mockUseSignal.mockReturnValue({
    data: undefined,
    isLoading: false,
    isError: false,
    error: null,
    refetch: vi.fn(),
  });
});

afterEach(() => {
  vi.clearAllMocks();
});

describe('SignalDetail — Execute CTA', () => {
  it('renders the Execute CTA for BTC + consider long', () => {
    renderAt(42, mkSignal());
    const cta = screen.getByRole('link', { name: /execute this signal/i });
    expect(cta).toBeInTheDocument();
    expect(cta).toHaveAttribute('href', '/execute?signal_id=42');
  });

  it('renders the Execute CTA for ETH + consider short', () => {
    renderAt(
      7,
      mkSignal({
        id: 7,
        asset: 'ETH',
        ai_analysis: { ...SAMPLE_ANALYSIS, suggested_action: 'consider short' },
      }),
    );
    const cta = screen.getByRole('link', { name: /execute this signal/i });
    expect(cta).toHaveAttribute('href', '/execute?signal_id=7');
  });

  it('hides the CTA for MARKET signals (regime claims, not trade calls)', () => {
    renderAt(42, mkSignal({ asset: 'MARKET' }));
    expect(
      screen.queryByRole('link', { name: /execute this signal/i }),
    ).not.toBeInTheDocument();
  });

  it('hides the CTA when direction is wait', () => {
    renderAt(
      42,
      mkSignal({
        ai_analysis: { ...SAMPLE_ANALYSIS, suggested_action: 'wait' },
      }),
    );
    expect(
      screen.queryByRole('link', { name: /execute this signal/i }),
    ).not.toBeInTheDocument();
  });

  it('hides the CTA when AI analysis is null (AI failed)', () => {
    renderAt(42, mkSignal({ ai_analysis: null }));
    expect(
      screen.queryByRole('link', { name: /execute this signal/i }),
    ).not.toBeInTheDocument();
  });

  it('hides the CTA while the signal query is loading', () => {
    renderAt(42, undefined, { isLoading: true });
    expect(
      screen.queryByRole('link', { name: /execute this signal/i }),
    ).not.toBeInTheDocument();
  });
});
