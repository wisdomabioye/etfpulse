/**
 * Tests for the R4 Home components. HeroProofCard gets the most attention —
 * its result/excursion math must follow the real (fraction) units, not the
 * prototype's mock conventions.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { describe, expect, it } from 'vitest';

import type { HeroOutcome } from '../../api/types';
import { CalibrationTeaser } from './CalibrationTeaser';
import { DetectorsShowcase } from './DetectorsShowcase';
import { HeroProofCard } from './HeroProofCard';
import { LoopDiagram } from './LoopDiagram';

const outcome = (over: Partial<HeroOutcome> = {}): HeroOutcome => ({
  signal_id: 99,
  asset: 'BTC',
  signal_type: 'magnitude',
  direction: 'long',
  confidence: 8,
  headline: 'BTC flow z-score outlier',
  entry_price: '100',
  stop_price: '95',
  target_price: '115',
  price_at_signal: '100',
  max_favorable: '0.18',
  max_adverse: '0.05',
  evaluated_at: '2026-06-08T00:00:00Z',
  signal_created_at: '2026-06-05T00:00:00Z',
  ...over,
});

describe('LoopDiagram', () => {
  it('renders all six stages in order', () => {
    render(<LoopDiagram />);
    for (const k of ['Ingest', 'Detect', 'Analyze', 'Deliver', 'Execute', 'Score']) {
      expect(screen.getByText(k)).toBeInTheDocument();
    }
    expect(screen.getByText('01')).toBeInTheDocument();
    expect(screen.getByText('06')).toBeInTheDocument();
  });
});

describe('DetectorsShowcase', () => {
  it('renders all five detectors with their "catches" copy', () => {
    render(<DetectorsShowcase />);
    expect(screen.getByText('Flow Anomaly')).toBeInTheDocument();
    expect(screen.getByText('Regime Shift')).toBeInTheDocument();
    expect(screen.getByText('N-day streak breaks in net flow')).toBeInTheDocument();
  });
});

describe('HeroProofCard', () => {
  it('renders the target card with the % move to target (real fraction units)', () => {
    render(
      <MemoryRouter>
        <HeroProofCard outcome={outcome()} kind="target" />
      </MemoryRouter>,
    );
    expect(screen.getByText('◎ Last target hit')).toBeInTheDocument();
    // (115-100)/100 = +15.0%
    expect(screen.getByText('+15.0%')).toBeInTheDocument();
    // max fav 0.18 → +18.0%, max adv 0.05 → -5.0%
    expect(screen.getByText('+18.0%')).toBeInTheDocument();
    expect(screen.getByText('-5.0%')).toBeInTheDocument();
    expect(screen.getByText('$100.00')).toBeInTheDocument(); // entry, 2dp below $1000
  });

  it('renders the stop card with the bounded drawdown', () => {
    render(
      <MemoryRouter>
        <HeroProofCard outcome={outcome()} kind="stop" />
      </MemoryRouter>,
    );
    expect(screen.getByText('◇ Last stop saved')).toBeInTheDocument();
    // header result = -max_adverse*100 = -5.0%
    expect(screen.getAllByText('-5.0%').length).toBeGreaterThanOrEqual(1);
  });

  it('navigates to the signal on click', () => {
    render(
      <MemoryRouter initialEntries={['/']}>
        <Routes>
          <Route path="/" element={<HeroProofCard outcome={outcome({ signal_id: 7 })} kind="target" />} />
          <Route path="/signals/:id" element={<div>signal page</div>} />
        </Routes>
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByRole('button'));
    expect(screen.getByText('signal page')).toBeInTheDocument();
  });
});

describe('CalibrationTeaser', () => {
  it('renders the pitch + CTA even while data loads', () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <MemoryRouter>
        <QueryClientProvider client={client}>
          <CalibrationTeaser />
        </QueryClientProvider>
      </MemoryRouter>,
    );
    expect(screen.getByText('Does confidence 8 actually win 80%?')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /Open the proof surface/ })).toBeInTheDocument();
  });
});
