/**
 * OrderCostPreview tests (P2). Pins cost/fee/funding rendering + the
 * cap-headroom warning that pre-empts the backend 403.
 *
 *   - no usable price (market, no mark) → renders nothing.
 *   - limit order → notional + maker fee + total.
 *   - market order with a live mark → "est · live mark" + taker fee.
 *   - perps funding rate shown.
 *   - per-symbol cap breach → loss warning (the exact 403 the user hit).
 *   - daily cap breach → loss warning.
 *   - reduce-only → cap-exempt, no warning even when over.
 */

import { render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { AccountSummary, ExecutionLimits } from '../../api/execution';

const mockLimits = vi.fn();
const mockSummary = vi.fn();
const mockSpot = vi.fn();
vi.mock('../../hooks/useExecution', () => ({
  useExecutionLimits: () => mockLimits(),
  useAccountSummary: () => mockSummary(),
}));
vi.mock('../../api/queries', () => ({
  useSpotPrices: () => mockSpot(),
}));

import { OrderCostPreview } from './OrderCostPreview';

function summary(over: Partial<AccountSummary> = {}): { data: AccountSummary } {
  return {
    data: {
      spot_balances: [],
      fee: { maker_rate: '0.0002', taker_rate: '0.0005' },
      mark_prices: [
        {
          symbol: 'BTCUSDT',
          asset: 'BTC',
          mark_price: '65000',
          funding_rate: '0.0001',
          next_funding_time: 0,
        },
      ],
      ...over,
    },
  };
}

function limits(over: Partial<ExecutionLimits> = {}): { data: ExecutionLimits } {
  return {
    data: {
      max_open_orders: 5,
      open_orders_used: 0,
      daily_notional_cap: '10000',
      daily_notional_used: '0',
      per_symbol_cap: '5000',
      per_symbol_used: '0',
      asset: 'BTC',
      max_leverage: 5,
      ...over,
    },
  };
}

const PROPS = {
  asset: 'BTC',
  isPerps: true,
  orderType: 'limit' as const,
  reduceOnly: false,
  size: '0.01',
  price: '65000',
};

describe('OrderCostPreview', () => {
  beforeEach(() => {
    // Default spot feed available; spot-specific tests override.
    mockSpot.mockReturnValue({ data: { btc: 65000, eth: 3000 } });
  });

  it('renders nothing when there is no usable price (perps market, no mark)', () => {
    mockLimits.mockReturnValue(limits());
    mockSummary.mockReturnValue(summary({ mark_prices: [] }));
    const { container } = render(
      <OrderCostPreview {...PROPS} orderType="market" price="" />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it('renders notional + maker fee + total for a limit order', () => {
    mockLimits.mockReturnValue(limits());
    mockSummary.mockReturnValue(summary());
    render(<OrderCostPreview {...PROPS} />);
    expect(screen.getByText('Notional')).toBeInTheDocument();
    expect(screen.getByText('$650.00')).toBeInTheDocument(); // 0.01 × 65000
    expect(screen.getByText(/Est. fee \(maker\)/)).toBeInTheDocument();
    expect(screen.getByText('Total')).toBeInTheDocument();
  });

  it('uses the live mark + taker fee for a perps market order', () => {
    mockLimits.mockReturnValue(limits());
    mockSummary.mockReturnValue(summary());
    render(<OrderCostPreview {...PROPS} orderType="market" price="" />);
    expect(screen.getByText(/est · live mark/)).toBeInTheDocument();
    expect(screen.getByText(/Est. fee \(taker\)/)).toBeInTheDocument();
  });

  it('falls back to the spot feed for a SPOT market order', () => {
    // account-summary has no spot mark; the BTC/ETH spot feed supplies the ref.
    mockLimits.mockReturnValue(limits());
    mockSummary.mockReturnValue(summary({ mark_prices: [] }));
    render(<OrderCostPreview {...PROPS} isPerps={false} orderType="market" price="" />);
    expect(screen.getByText('$650.00')).toBeInTheDocument(); // 0.01 × 65000 spot
    expect(screen.getByText(/est · spot/)).toBeInTheDocument();
  });

  it('hides a SPOT market order when the spot feed is unavailable', () => {
    mockLimits.mockReturnValue(limits());
    mockSummary.mockReturnValue(summary({ mark_prices: [] }));
    mockSpot.mockReturnValue({ data: undefined });
    const { container } = render(
      <OrderCostPreview {...PROPS} isPerps={false} orderType="market" price="" />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it('shows the perps funding rate', () => {
    mockLimits.mockReturnValue(limits());
    mockSummary.mockReturnValue(summary());
    render(<OrderCostPreview {...PROPS} />);
    expect(screen.getByText(/Funding \(perps\)/)).toBeInTheDocument();
  });

  it('warns when the per-symbol cap would be breached', () => {
    mockLimits.mockReturnValue(limits({ per_symbol_used: '4500' })); // +650 → 5150 > 5000
    mockSummary.mockReturnValue(summary());
    render(<OrderCostPreview {...PROPS} />);
    expect(screen.getByText(/Exceeds the BTC 24h cap/)).toBeInTheDocument();
  });

  it('warns when the daily cap would be breached (per-symbol fine)', () => {
    mockLimits.mockReturnValue(
      limits({ daily_notional_used: '9500', per_symbol_cap: '99999' }),
    ); // +650 → 10150 > 10000
    mockSummary.mockReturnValue(summary());
    render(<OrderCostPreview {...PROPS} />);
    expect(screen.getByText(/Exceeds your 24h notional cap/)).toBeInTheDocument();
  });

  it('reduce-only is cap-exempt — no warning even when over', () => {
    mockLimits.mockReturnValue(limits({ per_symbol_used: '4900' }));
    mockSummary.mockReturnValue(summary());
    render(<OrderCostPreview {...PROPS} reduceOnly />);
    expect(screen.queryByText(/Exceeds/)).not.toBeInTheDocument();
  });
});
