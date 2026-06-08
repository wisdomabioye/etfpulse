/**
 * Backtest results card unit tests. Page-level integration is in
 * `pages/admin/Backtest.test.tsx`; here we pin the cell-rendering
 * edge cases (empty per_detector, 0/null hit_rate, formatted percent).
 */

import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';

import type { BacktestReport } from '../../api/backtest';

import { BacktestResultsCard } from './BacktestResultsCard';

function mkReport(overrides: Partial<BacktestReport> = {}): BacktestReport {
  return {
    start: '2026-04-01',
    end: '2026-04-07',
    ai_prompt_version: 'v3',
    detector_configs: {},
    counters: {},
    per_detector: [],
    outcomes: [],
    ...overrides,
  };
}

function renderCard(report: BacktestReport) {
  return render(
    <MemoryRouter>
      <BacktestResultsCard report={report} />
    </MemoryRouter>,
  );
}

describe('BacktestResultsCard', () => {
  it('renders the empty-state copy when per_detector is empty', () => {
    renderCard(mkReport());
    expect(
      screen.getByText(/no detector data in the report/i),
    ).toBeInTheDocument();
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
  });

  it('renders a percent for hit_rate and "—" for null hit_rate', () => {
    renderCard(
      mkReport({
        per_detector: [
          {
            detector_name: 'flow_anomaly',
            n_hits: 3,
            n_scored: 3,
            wins: 2,
            losses: 1,
            hit_rate: 0.6667,
          },
          {
            detector_name: 'magnitude',
            n_hits: 0,
            n_scored: 0,
            wins: 0,
            losses: 0,
            hit_rate: null,
          },
        ],
      }),
    );
    expect(screen.getByText('66.7%')).toBeInTheDocument();
    // The null hit_rate cell renders the dash glyph. There are several
    // dashes across the magnitude row (n_hits/n_scored/wins/losses also
    // render as 0 → dash); assert presence rather than count.
    expect(screen.getAllByText('—').length).toBeGreaterThan(0);
  });

  it('cross-links each row to /track-record', () => {
    // The link target is the bare /track-record page — query-param
    // filtering would be silently ignored (the TrackRecord page reads
    // filters from local React state, not useSearchParams), so the
    // link delivers the operator to the same surface and they apply
    // the detector filter in the page's own filter row.
    renderCard(
      mkReport({
        per_detector: [
          {
            detector_name: 'divergence',
            n_hits: 1,
            n_scored: 1,
            wins: 1,
            losses: 0,
            hit_rate: 1.0,
          },
        ],
      }),
    );
    const link = screen.getByRole('link', { name: /live/i });
    expect(link).toHaveAttribute('href', '/track-record');
    expect(link).toHaveAttribute(
      'title',
      expect.stringContaining('divergence'),
    );
  });

  it('shows the prompt version in the header', () => {
    renderCard(mkReport({ ai_prompt_version: 'v5' }));
    expect(screen.getByText(/prompt v5/i)).toBeInTheDocument();
  });
});
