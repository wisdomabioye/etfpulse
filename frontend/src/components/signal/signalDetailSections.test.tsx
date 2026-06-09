/**
 * Tests for the prototype-ported detail sections: ConfirmationSection (factor
 * tiles) + OutcomeSection (pending dynamic countdown / evaluated grids).
 */

import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import type { FactorVote, SignalOutcome } from '../../api/types';
import { ConfirmationSection } from './ConfirmationSection';
import { OutcomeSection } from './OutcomeSection';

const outcome = (over: Partial<SignalOutcome> = {}): SignalOutcome => ({
  entry_price: 100,
  stop_price: 95,
  target_price: 115,
  price_at_signal: 100,
  price_after_24h: 104,
  price_after_72h: 110,
  price_at_validity_end: 110,
  window_hours: 72,
  scoring_version: 'v2',
  hit_target: true,
  hit_stop: false,
  max_favorable: 0.12,
  max_adverse: 0.03,
  evaluated_at: '2026-06-08T00:00:00Z',
  composite_return_pct: null,
  ...over,
});

describe('ConfirmationSection', () => {
  it('renders titled card + a tile per factor with vote pips', () => {
    const votes: Record<string, FactorVote> = {
      price: { vote: 1, reason: 'Price rose with the call' },
      regime: { vote: -1 },
      news: { vote: 0 },
    };
    render(<ConfirmationSection score={1} votes={votes} />);
    expect(screen.getByText('Multi-factor voting · 1/3')).toBeInTheDocument();
    expect(screen.getByText('Price')).toBeInTheDocument();
    expect(screen.getByText('Price rose with the call')).toBeInTheDocument();
    expect(screen.getByText('✓')).toBeInTheDocument(); // price confirms
    expect(screen.getByText('✗')).toBeInTheDocument(); // regime counters
    expect(screen.getByText('·')).toBeInTheDocument(); // news abstains
    // vote-derived fallback text when no reason string
    expect(screen.getByText('Counters the signal direction.')).toBeInTheDocument();
  });
});

describe('OutcomeSection — pending', () => {
  it('renders a LIVE countdown from expires_at − now', () => {
    const expiresAt = new Date(Date.now() + 70 * 3_600_000).toISOString();
    const createdAt = new Date(Date.now() - 2 * 3_600_000).toISOString();
    render(
      <OutcomeSection
        outcome={null}
        expiresAt={expiresAt}
        createdAt={createdAt}
        priceAtCreation={84200}
        direction="long"
      />,
    );
    expect(screen.getByText(/Pending — evaluates at the/)).toBeInTheDocument();
    // dynamic remaining (≈2d 22h from now), not a hardcoded "2d"
    expect(screen.getByText(/price at signal = \$84,200 · ~.+remaining/)).toBeInTheDocument();
  });
});

describe('OutcomeSection — evaluated', () => {
  it('single-asset long: directional return + target verdict + excursions', () => {
    render(
      <OutcomeSection
        outcome={outcome()}
        expiresAt={null}
        createdAt="2026-06-05T00:00:00Z"
        priceAtCreation={100}
        direction="long"
      />,
    );
    expect(screen.getByText('+10.00%')).toBeInTheDocument(); // (110-100)/100
    expect(screen.getByText('✓ Target hit')).toBeInTheDocument();
    expect(screen.getByText('+12.0% / -3.0%')).toBeInTheDocument();
  });

  it('single-asset short flips the return sign', () => {
    render(
      <OutcomeSection
        outcome={outcome({ price_at_validity_end: 90 })}
        expiresAt={null}
        createdAt="2026-06-05T00:00:00Z"
        priceAtCreation={100}
        direction="short"
      />,
    );
    expect(screen.getByText('+10.00%')).toBeInTheDocument(); // (90-100)/100 * -1
  });

  it('MARKET composite: composite return + composite verdict', () => {
    render(
      <OutcomeSection
        outcome={outcome({ composite_return_pct: 0.024, entry_price: null, price_at_signal: null })}
        expiresAt={null}
        createdAt="2026-06-05T00:00:00Z"
        priceAtCreation={null}
        direction={null}
      />,
    );
    expect(screen.getByText('+2.40%')).toBeInTheDocument();
    expect(screen.getByText('✓ Composite hit')).toBeInTheDocument();
    expect(screen.getByText('Composite return')).toBeInTheDocument();
  });
});
