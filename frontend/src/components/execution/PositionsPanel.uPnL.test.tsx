/**
 * PositionsPanel Mark + uPnL tests (P1).
 *
 * Pins the live-price resolver + uPnL rendering:
 *   - perps row resolves its mark from the account-summary mark prices;
 *     a profitable long shows a green "+" uPnL.
 *   - spot row resolves its mark from the spot feed; uPnL uses it.
 *   - a row with no available mark renders "—" for Mark + uPnL (never NaN).
 *
 * The pure uPnL math is covered in `lib/positionMath.test.ts`; this file
 * pins the wiring (which feed → which row) + the "—" fallback.
 */

import { render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import type { PositionOut } from '../../api/execution';

const mockPositions = vi.fn();
const mockSummary = vi.fn();
const mockSpot = vi.fn();

vi.mock('wagmi', () => ({
  useSignTypedData: () => ({ signTypedDataAsync: vi.fn() }),
}));
vi.mock('../../api/queries', () => ({
  useSpotPrices: () => mockSpot(),
}));
vi.mock('../../hooks/useExecution', () => ({
  usePositions: () => mockPositions(),
  useAccountSummary: () => mockSummary(),
  useClosePosition: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useSubmitNew: () => ({ mutateAsync: vi.fn(), isPending: false }),
}));

import { PositionsSection } from './PositionsPanel';

function pos(over: Partial<PositionOut> = {}): PositionOut {
  return {
    id: 1,
    user_id: 1,
    venue: 'sodex_perps',
    asset: 'BTC',
    side: 'long',
    size: '0.01',
    entry_price: '60000',
    stop_loss: null,
    take_profit: null,
    status: 'open',
    paper_trade: false,
    leverage: '3',
    opened_at: '2026-01-01T00:00:00Z',
    closed_at: null,
    close_price: null,
    realized_pnl: null,
    ...over,
  };
}

const SUMMARY_WITH_MARK = {
  data: {
    spot_balances: [],
    fee: null,
    mark_prices: [
      { symbol: 'BTCUSDT', asset: 'BTC', mark_price: '65000', funding_rate: null, next_funding_time: 0 },
    ],
  },
};
const SPOT = { data: { btc: 65000, eth: 3000 } };

describe('PositionsPanel Mark + uPnL', () => {
  it('resolves perps mark from account-summary and shows a green profit', () => {
    mockPositions.mockReturnValue({ data: { items: [pos()] }, isLoading: false, isError: false });
    mockSummary.mockReturnValue(SUMMARY_WITH_MARK);
    mockSpot.mockReturnValue({ data: undefined });
    render(<PositionsSection />);
    const row = screen.getByRole('button', { name: /Close BTC long position/i }).closest('tr')!;
    // mark 65000 vs entry 60000 → +$50 on 0.01 size.
    expect(within(row).getByText('$65,000')).toBeInTheDocument();
    expect(row.textContent).toContain('+$50.00');
  });

  it('resolves a spot row mark from the spot feed', () => {
    mockPositions.mockReturnValue({
      data: { items: [pos({ venue: 'sodex_spot' })] },
      isLoading: false,
      isError: false,
    });
    mockSummary.mockReturnValue({ data: undefined });
    mockSpot.mockReturnValue(SPOT);
    render(<PositionsSection />);
    const row = screen.getByRole('button', { name: /Close BTC long position/i }).closest('tr')!;
    expect(within(row).getByText('$65,000')).toBeInTheDocument();
    expect(row.textContent).toContain('+$50.00');
  });

  it('renders "—" for Mark + uPnL when no price is available', () => {
    mockPositions.mockReturnValue({
      data: { items: [pos({ venue: 'sodex_spot', asset: 'BTC' })] },
      isLoading: false,
      isError: false,
    });
    mockSummary.mockReturnValue({ data: undefined });
    mockSpot.mockReturnValue({ data: undefined }); // spot feed down
    render(<PositionsSection />);
    const row = screen.getByRole('button', { name: /Close BTC long position/i }).closest('tr')!;
    // Two em-dash cells (Mark + uPnL); entry still shows.
    const dashes = within(row).getAllByText('—');
    expect(dashes.length).toBeGreaterThanOrEqual(2);
  });
});
