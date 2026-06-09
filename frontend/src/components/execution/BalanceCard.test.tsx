/**
 * BalanceCard tests (P1). Pins the account-summary balance render branches.
 *
 *   - loading / error / empty states.
 *   - balances filtered to total>0, locked detail shown only when locked>0.
 *   - paper caveat shown only when paper=true.
 */

import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import type { AccountSummary } from '../../api/execution';

const mockSummary = vi.fn();
vi.mock('../../hooks/useExecution', () => ({
  useAccountSummary: () => mockSummary(),
}));

import { BalanceCard } from './BalanceCard';

function makeSummary(over: Partial<AccountSummary> = {}): AccountSummary {
  return {
    spot_balances: [
      { asset: 'BTC', total: '1.5', locked: '0.5', available: '1.0' },
      { asset: 'USDT', total: '2000', locked: '0', available: '2000' },
      { asset: 'ETH', total: '0', locked: '0', available: '0' }, // filtered (total 0)
    ],
    fee: null,
    mark_prices: [],
    ...over,
  };
}

describe('BalanceCard', () => {
  it('shows a loading state', () => {
    mockSummary.mockReturnValue({ data: undefined, isLoading: true, isError: false });
    render(<BalanceCard paper={false} />);
    expect(screen.getByText(/loading balance/i)).toBeInTheDocument();
  });

  it('shows an error state', () => {
    mockSummary.mockReturnValue({ data: undefined, isLoading: false, isError: true });
    render(<BalanceCard paper={false} />);
    expect(screen.getByText(/gateway didn't respond/i)).toBeInTheDocument();
  });

  it('shows an empty state when no positive balances', () => {
    mockSummary.mockReturnValue({
      data: makeSummary({ spot_balances: [{ asset: 'ETH', total: '0', locked: '0', available: '0' }] }),
      isLoading: false,
      isError: false,
    });
    render(<BalanceCard paper={false} />);
    expect(screen.getByText(/no spot balance/i)).toBeInTheDocument();
  });

  it('renders positive balances and the locked breakdown only when locked', () => {
    mockSummary.mockReturnValue({ data: makeSummary(), isLoading: false, isError: false });
    render(<BalanceCard paper={false} />);
    // BTC available 1.0 (trimmed → "1") with locked breakdown.
    expect(screen.getByText(/1.5 total · 0.5 locked/)).toBeInTheDocument();
    // USDT has no lock → no breakdown line.
    expect(screen.queryByText(/2000 total · 0 locked/)).not.toBeInTheDocument();
    // ETH (total 0) is filtered out.
    expect(screen.queryByText('ETH')).not.toBeInTheDocument();
  });

  it('shows the paper caveat only in paper mode', () => {
    mockSummary.mockReturnValue({ data: makeSummary(), isLoading: false, isError: false });
    const { rerender } = render(<BalanceCard paper={false} />);
    expect(screen.queryByText(/paper orders are simulated/i)).not.toBeInTheDocument();
    rerender(<BalanceCard paper />);
    expect(screen.getByText(/paper orders are simulated/i)).toBeInTheDocument();
  });
});
