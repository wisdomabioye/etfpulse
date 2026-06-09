/**
 * Tests for the R5 Proof components. The realized-return helper gets the most
 * scrutiny — MARKET (composite) vs single-asset long/short must each carry the
 * right sign, and missing inputs must yield null (never a fabricated number).
 */

import { fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { describe, expect, it } from 'vitest';

import type { CalibrationCell } from '../charts';
import type { TrackRecordItem, TrackRecordSummary } from '../../api/types';
import { CalibTable } from './CalibTable';
import { OutcomeTable } from './OutcomeTable';
import { ProofStatBand } from './ProofStatBand';
import { horizonLabel, realizedReturnPct } from './outcomeRow';

const item = (over: Partial<TrackRecordItem> = {}): TrackRecordItem => ({
  id: 1,
  signal_id: 10,
  asset: 'BTC',
  signal_type: 'magnitude',
  direction: 'long',
  confidence: 7,
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
  composite_return_pct: null,
  evaluated_at: '2026-06-08T00:00:00Z',
  ...over,
});

describe('realizedReturnPct', () => {
  it('uses composite_return_pct for MARKET rows', () => {
    expect(realizedReturnPct(item({ composite_return_pct: 0.024, entry_price: null }))).toBeCloseTo(
      2.4,
      5,
    );
  });
  it('computes a long return from entry→validity-end', () => {
    expect(realizedReturnPct(item())).toBeCloseTo(10, 5); // (110-100)/100
  });
  it('flips the sign for a short (profits when price falls)', () => {
    expect(
      realizedReturnPct(item({ direction: 'short', price_at_validity_end: 90 })),
    ).toBeCloseTo(10, 5); // (90-100)/100 * -1
  });
  it('falls back to the 72h close when validity-end is null', () => {
    expect(realizedReturnPct(item({ price_at_validity_end: null, price_after_72h: 105 }))).toBeCloseTo(
      5,
      5,
    );
  });
  it('returns null when nothing computable', () => {
    expect(
      realizedReturnPct(
        item({ entry_price: null, composite_return_pct: null, price_at_validity_end: null, price_after_72h: null }),
      ),
    ).toBeNull();
  });
});

describe('horizonLabel', () => {
  it('buckets by scoring window', () => {
    expect(horizonLabel(null)).toBe('legacy');
    expect(horizonLabel(6)).toBe('scalp');
    expect(horizonLabel(72)).toBe('swing');
    expect(horizonLabel(168)).toBe('position');
  });
});

describe('ProofStatBand', () => {
  const summary: TrackRecordSummary = {
    total_evaluated: 204,
    targets_hit: 120,
    stops_hit: 60,
    targeted_count: 180,
    hit_rate_pct: 66.7,
    hit_rate_by_horizon: { scalp: null, swing: 70, position: 55, legacy: null },
    avg_confidence_hits: 7.4,
    avg_confidence_misses: 5.1,
  };
  it('renders the five real stats', () => {
    render(<ProofStatBand summary={summary} />);
    expect(screen.getByText('204')).toBeInTheDocument();
    expect(screen.getByText('66.7%')).toBeInTheDocument();
    expect(screen.getByText('120')).toBeInTheDocument();
    expect(screen.getByText('7.4/10 / 5.1/10')).toBeInTheDocument();
  });
});

describe('CalibTable', () => {
  const cells: CalibrationCell[] = [
    { hit: 0.18, ci_low: 0.1, ci_high: 0.28, n: 40, wins: 7, insufficient: false },
    { hit: 0, ci_low: 0, ci_high: 0, n: 4, wins: 1, insufficient: true },
  ];
  it('shows sufficient rows and an honest insufficient row', () => {
    render(<CalibTable cells={cells} />);
    expect(screen.getByText('1–2')).toBeInTheDocument();
    expect(screen.getByText('— insufficient (n=4)')).toBeInTheDocument();
  });
});

describe('OutcomeTable', () => {
  it('renders rows and navigates on click', () => {
    render(
      <MemoryRouter initialEntries={['/']}>
        <Routes>
          <Route path="/" element={<OutcomeTable rows={[item({ signal_id: 55 })]} />} />
          <Route path="/signals/:id" element={<div>signal 55</div>} />
        </Routes>
      </MemoryRouter>,
    );
    expect(screen.getByText('#55')).toBeInTheDocument();
    expect(screen.getByText('✓ target')).toBeInTheDocument();
    expect(screen.getByText('+10.00%')).toBeInTheDocument(); // realized return
    fireEvent.click(screen.getByText('#55'));
    expect(screen.getByText('signal 55')).toBeInTheDocument();
  });
});
