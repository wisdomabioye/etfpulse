/**
 * Tests for the regime history strip — renders one glyph cell per day from
 * the real `/regime/history` data (cache-seeded), newest data shown
 * chronologically, plus the legend. Empty data → honest "no history" line.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import type { ReactNode } from 'react';
import { describe, expect, it } from 'vitest';

import type { RegimeHistoryResponse } from '../../api/types';
import { RegimeHistoryStrip } from './RegimeHistoryStrip';

function withSeed(data: RegimeHistoryResponse): ReactNode {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  client.setQueryData(['regime', 'history', 8], data);
  return (
    <QueryClientProvider client={client}>
      <RegimeHistoryStrip />
    </QueryClientProvider>
  );
}

describe('RegimeHistoryStrip', () => {
  it('renders a day cell per history item + the regime legend', () => {
    render(
      withSeed({
        history: [
          { date: '2026-06-08', regime: 'markup' },
          { date: '2026-06-07', regime: 'distribution' },
        ],
      }),
    );
    // Day-of-month labels (08, 07).
    expect(screen.getByText('08')).toBeInTheDocument();
    expect(screen.getByText('07')).toBeInTheDocument();
    // Legend lists all five regimes.
    expect(screen.getByText('Markup')).toBeInTheDocument();
    expect(screen.getByText('Accumulation')).toBeInTheDocument();
    expect(screen.getByText('Uncertain')).toBeInTheDocument();
  });

  it('shows an honest empty line when there is no history', () => {
    render(withSeed({ history: [] }));
    expect(screen.getByText('No history recorded yet.')).toBeInTheDocument();
  });
});
