/**
 * Render + interaction tests for the R2 charts. The signature CalibrationCurve
 * gets the most coverage: gridlines, bucket labels, insufficient "—" cells,
 * and the hover tooltip. Edge cases (empty sparkline, insufficient cells) are
 * asserted as negatives.
 */

import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import {
  Bar,
  CalibrationCurve,
  DetectorLeaderboard,
  HitRateBars,
  RegimeIndicator,
  RRBar,
  Sparkline,
} from './index';
import type { CalibrationCell, HitRateRow, LeaderboardRow } from './types';

const cell = (over: Partial<CalibrationCell>): CalibrationCell => ({
  hit: 0.6,
  ci_low: 0.5,
  ci_high: 0.7,
  n: 40,
  wins: 24,
  insufficient: false,
  ...over,
});

describe('Sparkline', () => {
  it('renders an svg path for data', () => {
    const { container } = render(<Sparkline data={[1, 3, 2, 5, 4]} />);
    expect(container.querySelector('svg')).toBeInTheDocument();
    expect(container.querySelectorAll('path').length).toBeGreaterThanOrEqual(1);
  });
  it('renders nothing for empty data', () => {
    const { container } = render(<Sparkline data={[]} />);
    expect(container.querySelector('svg')).not.toBeInTheDocument();
  });
});

describe('Bar', () => {
  it('clamps the fill width to 0–100%', () => {
    const { container } = render(<Bar value={5} max={1} />);
    const fill = container.firstElementChild?.firstElementChild as HTMLElement;
    expect(fill.style.width).toBe('100%');
  });
});

describe('CalibrationCurve', () => {
  const cells = [
    cell({ hit: 0.18 }),
    cell({ insufficient: true }),
    cell({ hit: 0.55 }),
    cell({ hit: 0.78 }),
    cell({ hit: 0.92 }),
  ];

  it('renders the labeled plot with bucket labels', () => {
    render(<CalibrationCurve cells={cells} horizon="swing" />);
    const svg = screen.getByRole('img');
    expect(svg).toHaveAttribute('aria-label', expect.stringContaining('swing'));
    expect(screen.getByText('1–2')).toBeInTheDocument();
    expect(screen.getByText('9–10')).toBeInTheDocument();
  });

  it('renders a "—" for an insufficient bucket', () => {
    render(<CalibrationCurve cells={cells} horizon="swing" />);
    expect(screen.getAllByText('—').length).toBeGreaterThanOrEqual(1);
  });

  it('shows a tooltip on point hover and hides it on leave', () => {
    const { container } = render(<CalibrationCurve cells={cells} horizon="position" />);
    // A visible point is a filled circle (win/loss); hover its parent <g>.
    const point = Array.from(container.querySelectorAll('circle')).find((c) => {
      const f = c.getAttribute('fill') ?? '';
      return f.includes('var(--win)') || f.includes('var(--loss)');
    });
    expect(point).toBeTruthy();
    const group = point!.closest('g')!;
    fireEvent.mouseEnter(group);
    expect(screen.getByText(/hit/)).toBeInTheDocument();
    expect(screen.getByText(/95% CI/)).toBeInTheDocument();
    fireEvent.mouseLeave(group);
    expect(screen.queryByText(/95% CI/)).not.toBeInTheDocument();
  });
});

describe('HitRateBars', () => {
  it('renders a labeled bar per horizon', () => {
    const data: HitRateRow[] = [
      { horizon: 'scalp', hit: 0.72, n: 30 },
      { horizon: 'swing', hit: 0.58, n: 50 },
      { horizon: 'position', hit: 0.4, n: 12 },
    ];
    render(<HitRateBars data={data} />);
    expect(screen.getByText('Scalp')).toBeInTheDocument();
    expect(screen.getByText('72.0%')).toBeInTheDocument();
    expect(screen.getByText('n=50')).toBeInTheDocument();
  });
});

describe('DetectorLeaderboard', () => {
  it('renders ranked detector rows with hit + CI', () => {
    const data: LeaderboardRow[] = [
      { key: 'magnitude', hit: 0.71, ci_low: 0.6, ci_high: 0.82, n: 60 },
      { key: 'flow_anomaly', hit: 0.49, ci_low: 0.38, ci_high: 0.6, n: 40 },
    ];
    render(<DetectorLeaderboard data={data} />);
    expect(screen.getByText('Magnitude')).toBeInTheDocument();
    expect(screen.getByText('Flow Anomaly')).toBeInTheDocument();
    expect(screen.getByText('01')).toBeInTheDocument();
    expect(screen.getByText('71.0%')).toBeInTheDocument();
  });
});

describe('RRBar', () => {
  it('renders entry/stop/target markers and the R:R ratio', () => {
    render(<RRBar entry={100} stop={95} target={115} />);
    expect(screen.getByText('Stop')).toBeInTheDocument();
    expect(screen.getByText('Entry')).toBeInTheDocument();
    expect(screen.getByText('Target')).toBeInTheDocument();
    // |115-100| / |100-95| = 3.00
    expect(screen.getByText(/1 : 3\.00/)).toBeInTheDocument();
  });
});

describe('RegimeIndicator', () => {
  it('renders the active regime label, glyph, and confidence', () => {
    render(<RegimeIndicator state="markup" confidence={7} />);
    expect(screen.getByText('Markup')).toBeInTheDocument();
    expect(screen.getByText('confidence 7/10')).toBeInTheDocument();
    expect(screen.getByLabelText('Market regime: Markup')).toBeInTheDocument();
  });
});
