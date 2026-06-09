/**
 * Tests for the NEW behaviors added when reskinning the existing primitives
 * (R1 Slice B). Pre-existing behavior is covered by the page tests that
 * render through these components; here we pin the branches the reskin added
 * — Button variants/icon/destructive, StatTile accent/sub/live, Callout
 * title/icon/tone-map, SectionHeader kicker/sub, EmptyState, FilterPill.
 */

import { fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { Button, Callout, EmptyState, FilterPill, SectionHeader, StatTile } from './index';

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('Button (reskin)', () => {
  it('renders the destructive treatment with a loss border', () => {
    render(<Button destructive>Delete</Button>);
    const btn = screen.getByRole('button', { name: 'Delete' });
    expect(btn.className).toContain('text-loss');
    expect(btn.getAttribute('style')).toContain('color-mix');
  });
  it('renders an icon before the label and stretches when full', () => {
    render(
      <Button icon={<span data-testid="ic">★</span>} full>
        Go
      </Button>,
    );
    expect(screen.getByTestId('ic')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /go/i }).className).toContain('w-full');
  });
  it('supports the outline variant and lg size', () => {
    render(
      <Button variant="outline" size="lg">
        Browse
      </Button>,
    );
    const btn = screen.getByRole('button', { name: 'Browse' });
    expect(btn.className).toContain('border-line-3');
    expect(btn.className).toContain('text-[14px]');
  });
  it('renders as a router link', () => {
    render(
      <MemoryRouter>
        <Button as="link" to="/signals">
          Feed
        </Button>
      </MemoryRouter>,
    );
    expect(screen.getByRole('link', { name: 'Feed' })).toHaveAttribute('href', '/signals');
  });
  it('renders as a plain anchor', () => {
    render(
      <Button as="a" href="https://t.me/bot" target="_blank" rel="noreferrer">
        Telegram
      </Button>,
    );
    const a = screen.getByRole('link', { name: 'Telegram' });
    expect(a).toHaveAttribute('href', 'https://t.me/bot');
    expect(a).toHaveAttribute('target', '_blank');
  });
});

describe('StatTile (reskin)', () => {
  it('renders accent + sub and a static value', () => {
    render(<StatTile label="Hit rate" value="62%" accent sub="n=204" />);
    expect(screen.getByText('Hit rate')).toBeInTheDocument();
    const val = screen.getByText('62%');
    expect(val.className).toContain('text-acc-hi');
    expect(screen.getByText('n=204')).toBeInTheDocument();
  });
  it('formats a live numeric value (snapped under reduced motion)', () => {
    vi.stubGlobal('matchMedia', (q: string) => ({
      matches: true, // reduced motion → useLiveNumber snaps to target, no rAF
      media: q,
      addEventListener: () => {},
      removeEventListener: () => {},
    }));
    render(<StatTile label="Signals" value={1500} live />);
    expect(screen.getByText('1,500')).toBeInTheDocument();
  });
  it('formats a small live value to one decimal', () => {
    vi.stubGlobal('matchMedia', (q: string) => ({
      matches: true,
      media: q,
      addEventListener: () => {},
      removeEventListener: () => {},
    }));
    render(<StatTile label="Avg conf" value={6.4} live />);
    expect(screen.getByText('6.4')).toBeInTheDocument();
  });
  it('renders the trend arrow + color', () => {
    render(<StatTile label="Flows" value="+2.2%" trend={{ dir: 'up', value: '12%' }} />);
    const trend = screen.getByText(/12%/);
    expect(trend.textContent).toContain('↑');
    expect(trend.className).toContain('text-win');
  });
});

describe('Callout (reskin)', () => {
  it('renders title + icon and tints by tone (info → accent)', () => {
    const { container } = render(
      <Callout tone="info" title="Heads up" icon={<span data-testid="ic">i</span>}>
        body text
      </Callout>,
    );
    expect(screen.getByText('Heads up')).toBeInTheDocument();
    expect(screen.getByTestId('ic')).toBeInTheDocument();
    // The styled root is the outermost element.
    expect(container.firstElementChild?.getAttribute('style')).toContain('var(--acc)');
  });
  it('maps neg tone to the loss token', () => {
    const { container } = render(<Callout tone="neg">danger</Callout>);
    expect(container.firstElementChild?.getAttribute('style')).toContain('var(--loss)');
  });
});

describe('SectionHeader (reskin)', () => {
  it('renders kicker, title, sub, and action', () => {
    render(
      <SectionHeader
        kicker="OVERVIEW"
        title="Most recent"
        sub="The latest signals."
        action={<button>All →</button>}
      />,
    );
    expect(screen.getByText('OVERVIEW')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Most recent' })).toBeInTheDocument();
    expect(screen.getByText('The latest signals.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'All →' })).toBeInTheDocument();
  });
});

describe('EmptyState (reskin)', () => {
  it('renders title, hint, and action', () => {
    render(<EmptyState title="No matches" hint="Widen the filters." action={<button>Reset</button>} />);
    expect(screen.getByText('No matches')).toBeInTheDocument();
    expect(screen.getByText('Widen the filters.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Reset' })).toBeInTheDocument();
  });
});

describe('FilterPill (reskin)', () => {
  it('reflects active state and fires onClick', () => {
    const onClick = vi.fn();
    const { rerender } = render(
      <FilterPill active onClick={onClick}>
        BTC
      </FilterPill>,
    );
    const pill = screen.getByRole('button', { name: 'BTC' });
    expect(pill).toHaveAttribute('aria-pressed', 'true');
    expect(pill.className).toContain('bg-acc-soft');
    fireEvent.click(pill);
    expect(onClick).toHaveBeenCalledOnce();

    rerender(<FilterPill onClick={onClick}>BTC</FilterPill>);
    expect(screen.getByRole('button', { name: 'BTC' })).toHaveAttribute('aria-pressed', 'false');
  });
});
