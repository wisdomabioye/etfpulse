/**
 * Render tests for the R1 prototype primitives.
 *
 * Each primitive gets: a render-without-crash + key content assertion
 * (positive), a variant/branch assertion, and — for the data-driven color
 * components — a check that the correct design token reaches the inline
 * `style` attribute (the colors come from `colors.ts` tokens, so a wrong
 * token is a real regression). Interactive primitives (Logo, Tabs) get a
 * click/callback test.
 */

import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import {
  ActionTag,
  AssetBadge,
  Card,
  ConfidenceBadge,
  ConfirmationPips,
  DetectorBadge,
  DetectorIcon,
  Logo,
  Tabs,
} from './index';

describe('Logo', () => {
  it('renders the wordmark as a span when not clickable', () => {
    render(<Logo />);
    expect(screen.getByText('ETFPulse')).toBeInTheDocument();
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });
  it('renders a button and fires onClick when clickable', () => {
    const onClick = vi.fn();
    render(<Logo onClick={onClick} />);
    const btn = screen.getByRole('button', { name: /etfpulse home/i });
    fireEvent.click(btn);
    expect(onClick).toHaveBeenCalledOnce();
  });
});

describe('Card', () => {
  it('renders children and spreads rest props', () => {
    render(
      <Card data-testid="c" accent hover>
        body
      </Card>,
    );
    const el = screen.getByTestId('c');
    expect(el).toHaveTextContent('body');
    expect(el.className).toContain('border-l-acc');
    expect(el.className).toContain('hover:border-acc-line');
    expect(el.className).toContain('p-5');
  });
  it('drops padding when pad is false', () => {
    render(
      <Card data-testid="c" pad={false}>
        x
      </Card>,
    );
    expect(screen.getByTestId('c').className).not.toContain('p-5');
  });
  it('renders the plain default (no hover/accent) and appends className', () => {
    render(
      <Card data-testid="c" className="extra">
        x
      </Card>,
    );
    const el = screen.getByTestId('c');
    expect(el.className).toContain('extra');
    expect(el.className).toContain('p-5'); // pad defaults true
    expect(el.className).not.toContain('hover:border-acc-line');
    expect(el.className).not.toContain('border-l-acc');
  });
});

describe('AssetBadge', () => {
  it('renders the symbol with the brand-color fill + ink text', () => {
    render(<AssetBadge asset="BTC" />);
    const el = screen.getByText('BTC');
    const style = el.getAttribute('style') ?? '';
    expect(style).toContain('var(--btc)');
    expect(style).toContain('var(--ink)');
  });
  it('uses the market token for MARKET', () => {
    render(<AssetBadge asset="MARKET" size="sm" />);
    expect(screen.getByText('MARKET').getAttribute('style')).toContain('var(--market)');
  });
  it('resolves a venue-symbol form (BTC-USD) to its base brand color', () => {
    // Execution orders/positions carry "BTC-USD" — it must still render in
    // BTC orange with readable ink text, not dark-on-no-fill.
    render(<AssetBadge asset="BTC-USD" />);
    const style = screen.getByText('BTC-USD').getAttribute('style') ?? '';
    expect(style).toContain('var(--btc)');
    expect(style).toContain('var(--ink)');
  });
  it('falls back to a neutral readable chip for an unknown ticker', () => {
    render(<AssetBadge asset="ZZZ" />);
    const el = screen.getByText('ZZZ');
    // No bright fill / ink text — uses the neutral surface + t1 text class.
    expect(el.getAttribute('style')).toBeNull();
    expect(el.className).toMatch(/bg-bg-3/);
    expect(el.className).toMatch(/text-t1/);
  });
});

describe('DetectorBadge', () => {
  it('renders the full label at md with the detector color + an icon', () => {
    const { container } = render(<DetectorBadge type="flow_anomaly" />);
    expect(screen.getByText('Flow Anomaly')).toBeInTheDocument();
    expect(container.querySelector('svg')).toBeInTheDocument();
    const badge = screen.getByText('Flow Anomaly');
    expect(badge.getAttribute('style')).toContain('var(--det-flow)');
  });
  it('renders the short label at sm', () => {
    render(<DetectorBadge type="magnitude" size="sm" />);
    expect(screen.getByText('Mag')).toBeInTheDocument();
  });
  it('hides the label when showLabel is false', () => {
    render(<DetectorBadge type="divergence" showLabel={false} />);
    expect(screen.queryByText('Divergence')).not.toBeInTheDocument();
  });
});

describe('DetectorIcon', () => {
  it('renders an svg for every detector type', () => {
    const types = [
      'flow_anomaly',
      'magnitude',
      'acceleration',
      'divergence',
      'regime_shift',
    ] as const;
    for (const t of types) {
      const { container, unmount } = render(<DetectorIcon type={t} />);
      expect(container.querySelector('svg')).toBeInTheDocument();
      unmount();
    }
  });
  it('defaults the stroke to the detector token and honors an override', () => {
    const { container, rerender } = render(<DetectorIcon type="regime_shift" />);
    expect(container.querySelector('svg')?.getAttribute('stroke')).toBe('var(--det-regime)');
    rerender(<DetectorIcon type="regime_shift" color="red" />);
    expect(container.querySelector('svg')?.getAttribute('stroke')).toBe('red');
  });
});

describe('ConfidenceBadge', () => {
  it('renders the inline chip with N/10', () => {
    render(<ConfidenceBadge value={8} />);
    expect(screen.getByText('8')).toBeInTheDocument();
    expect(screen.getByText('/10')).toBeInTheDocument();
  });
  it('renders the lg panel with a confidence label', () => {
    render(<ConfidenceBadge value={3} size="lg" />);
    expect(screen.getByText('confidence')).toBeInTheDocument();
    expect(screen.getByText('3')).toBeInTheDocument();
  });
});

describe('ConfirmationPips', () => {
  it('renders three pips and a descriptive title', () => {
    const { container } = render(<ConfirmationPips value={2} />);
    const wrap = container.firstElementChild as HTMLElement;
    expect(wrap).toHaveAttribute('title', 'Confirmation 2/3');
    expect(wrap.children).toHaveLength(3);
    // First two filled (bg-acc), last empty (bg-line-3).
    expect(wrap.children[0].className).toContain('bg-acc');
    expect(wrap.children[2].className).toContain('bg-line-3');
  });
});

describe('ActionTag', () => {
  it.each([
    ['consider long', 'Long', '▲', '--win'],
    ['consider short', 'Short', '▼', '--loss'],
    ['wait', 'Wait', '■', '--warn'],
  ] as const)('maps %s → %s', (action, label, arrow, token) => {
    render(<ActionTag action={action} />);
    expect(screen.getByText(label)).toBeInTheDocument();
    expect(screen.getByText(arrow)).toBeInTheDocument();
    // The tag wrapper carries the tone token in its inline style.
    expect(screen.getByText(label).closest('span')?.getAttribute('style')).toContain(
      `var(${token})`,
    );
  });
});

describe('Tabs', () => {
  const tabs = [
    { value: 'scalp', label: 'Scalp' },
    { value: 'swing', label: 'Swing', sub: '12' },
  ] as const;

  it('marks the active tab and shows the sub-count', () => {
    render(<Tabs tabs={tabs} value="scalp" onChange={() => {}} />);
    expect(screen.getByRole('tab', { name: /scalp/i })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByRole('tab', { name: /swing/i })).toHaveAttribute('aria-selected', 'false');
    expect(screen.getByText('12')).toBeInTheDocument();
  });
  it('fires onChange with the tab value', () => {
    const onChange = vi.fn();
    render(<Tabs tabs={tabs} value="scalp" onChange={onChange} />);
    fireEvent.click(screen.getByRole('tab', { name: /swing/i }));
    expect(onChange).toHaveBeenCalledWith('swing');
  });
});
