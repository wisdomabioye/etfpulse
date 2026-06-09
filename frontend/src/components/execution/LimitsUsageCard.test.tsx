/**
 * LimitsUsageCard tests (P0). Pins every render branch of the caps/usage
 * card and the over-cap visual state.
 *
 *   - loading → "Loading limits…".
 *   - error → "still apply at submit" reassurance.
 *   - data, no asset → open-orders + 24h bars, NO per-symbol bar.
 *   - data, asset scoped → per-symbol bar present.
 *   - over a cap → the value renders in the loss tone.
 */

import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import type { ExecutionLimits } from '../../api/execution';

const mockLimits = vi.fn();
vi.mock('../../hooks/useExecution', () => ({
  useExecutionLimits: () => mockLimits(),
}));

import { LimitsUsageCard } from './LimitsUsageCard';

function makeLimits(over: Partial<ExecutionLimits> = {}): ExecutionLimits {
  return {
    max_open_orders: 5,
    open_orders_used: 2,
    daily_notional_cap: '10000',
    daily_notional_used: '4000',
    per_symbol_cap: '5000',
    per_symbol_used: '3000',
    asset: 'BTC',
    max_leverage: 5,
    ...over,
  };
}

describe('LimitsUsageCard', () => {
  it('shows a loading state', () => {
    mockLimits.mockReturnValue({ data: undefined, isLoading: true, isError: false });
    render(<LimitsUsageCard />);
    expect(screen.getByText(/loading limits/i)).toBeInTheDocument();
  });

  it('shows a reassuring error state', () => {
    mockLimits.mockReturnValue({ data: undefined, isLoading: false, isError: true });
    render(<LimitsUsageCard />);
    expect(screen.getByText(/still apply at submit/i)).toBeInTheDocument();
  });

  it('renders open-orders + 24h bars and omits per-symbol when unscoped', () => {
    mockLimits.mockReturnValue({
      data: makeLimits({ asset: null, per_symbol_used: null }),
      isLoading: false,
      isError: false,
    });
    render(<LimitsUsageCard />);
    expect(screen.getByText('Open orders')).toBeInTheDocument();
    expect(screen.getByText('24h notional')).toBeInTheDocument();
    expect(screen.queryByText(/BTC · 24h notional/)).not.toBeInTheDocument();
    expect(screen.getByText(/max 5× lev/)).toBeInTheDocument();
  });

  it('renders the per-symbol bar when scoped to an asset', () => {
    mockLimits.mockReturnValue({ data: makeLimits(), isLoading: false, isError: false });
    render(<LimitsUsageCard asset="BTC" />);
    expect(screen.getByText('BTC · 24h notional')).toBeInTheDocument();
  });

  it('renders the over-cap value in the loss tone', () => {
    mockLimits.mockReturnValue({
      data: makeLimits({ open_orders_used: 7 }), // 7 > cap 5
      isLoading: false,
      isError: false,
    });
    render(<LimitsUsageCard />);
    // The used/cap span carries text-loss when over.
    const over = screen.getByText('7', { exact: false });
    expect(over.className).toContain('text-loss');
  });

  it('treats exactly-at-cap as blocked (loss tone) — zero headroom', () => {
    // open_orders_used === max_open_orders (5/5): the next non-reduce order
    // is already denied by the gate (count >= max), so the bar must alarm.
    mockLimits.mockReturnValue({
      data: makeLimits({ open_orders_used: 5 }),
      isLoading: false,
      isError: false,
    });
    render(<LimitsUsageCard />);
    // Target the open-orders value span via its label sibling (a bare "5"
    // matches several elements — price values, "max 5× lev").
    const valueSpan = screen.getByText('Open orders').nextElementSibling;
    expect(valueSpan?.textContent).toContain('5 / 5');
    expect(valueSpan?.className).toContain('text-loss');
  });
});
