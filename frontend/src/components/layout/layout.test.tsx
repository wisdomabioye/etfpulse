/**
 * Render tests for the R3 layout shell. TopNav is exercised with the real
 * provider stack (Router + Query + Auth) in its loading/unauthed default —
 * which renders the grouped nav + "Connect wallet" CTA, with PriceStrip in
 * its placeholder state and RegimeGlance hidden (no data yet).
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen } from '@testing-library/react';
import type { ReactNode } from 'react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';

import { AuthProvider } from '../../auth/AuthContext';
import { Footer } from './Footer';
import { MobileTabBar } from './MobileTabBar';
import { TopNav } from './TopNav';

function withProviders(ui: ReactNode, initialPath = '/') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MemoryRouter initialEntries={[initialPath]}>
      <QueryClientProvider client={client}>
        <AuthProvider>{ui}</AuthProvider>
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

describe('TopNav', () => {
  it('renders the grouped nav and the unauthed CTA', () => {
    withProviders(<TopNav />);
    for (const label of ['Signals', 'Proof', 'Regime', 'Trade', 'Learn']) {
      expect(screen.getByRole('link', { name: new RegExp(label) })).toBeInTheDocument();
    }
    expect(screen.getByRole('link', { name: 'Connect wallet' })).toBeInTheDocument();
  });

  it('toggles the mobile menu', () => {
    withProviders(<TopNav />);
    const toggle = screen.getByRole('button', { name: 'Open menu' });
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    fireEvent.click(toggle);
    expect(screen.getByRole('button', { name: 'Close menu' })).toHaveAttribute(
      'aria-expanded',
      'true',
    );
    // Opening the panel renders a SECOND set of nav links (desktop + mobile).
    expect(screen.getAllByRole('link', { name: /Signals/ }).length).toBeGreaterThanOrEqual(2);
  });
});

describe('MobileTabBar', () => {
  it('renders the four primary tabs', () => {
    render(
      <MemoryRouter initialEntries={['/signals']}>
        <MobileTabBar />
      </MemoryRouter>,
    );
    for (const label of ['Home', 'Signals', 'Proof', 'Trade']) {
      expect(screen.getByRole('link', { name: label })).toBeInTheDocument();
    }
  });
});

describe('Footer', () => {
  it('renders the brand, columns, links, and disclaimer', () => {
    render(
      <MemoryRouter>
        <Footer />
      </MemoryRouter>,
    );
    expect(screen.getByText('ETFPulse')).toBeInTheDocument();
    expect(screen.getByText('Product')).toBeInTheDocument();
    expect(screen.getByText('Learn')).toBeInTheDocument();
    expect(screen.getByText('Methodology')).toBeInTheDocument();
    expect(screen.getByText(/No auto-trading/)).toBeInTheDocument();
    // External link opens in a new tab safely.
    const gh = screen.getByRole('link', { name: 'GitHub' });
    expect(gh).toHaveAttribute('rel', 'noopener noreferrer');
  });
});
