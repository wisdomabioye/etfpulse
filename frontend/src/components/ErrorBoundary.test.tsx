/**
 * ErrorBoundary smoke (#78.15).
 *
 * Pins the contract: rendering a child that throws displays the
 * fallback UI (not the original child). The Retry button is present.
 *
 * What's NOT tested here:
 *   - The reload() side effect — would require mocking window.location,
 *     which is read-only in jsdom. The Retry button's wiring is one
 *     line of code; visual inspection is enough.
 *   - Async errors (event handlers, fetch rejections) — those are NOT
 *     caught by ErrorBoundary by design; testing the absence is
 *     low-leverage.
 */

import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { ErrorBoundary } from './ErrorBoundary';

function ThrowingChild(): never {
  throw new Error('boom — synthetic test failure');
}

describe('ErrorBoundary', () => {
  it('renders children normally when no error', () => {
    render(
      <ErrorBoundary>
        <div data-testid="ok">all good</div>
      </ErrorBoundary>,
    );
    expect(screen.getByTestId('ok')).toBeInTheDocument();
    expect(screen.getByText('all good')).toBeInTheDocument();
  });

  it('catches render-time errors and shows the fallback', () => {
    // React logs the caught error via console.error during render —
    // silence it so the test output isn't cluttered. The actual
    // log assertion (that we DO log) is a separate concern.
    const consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => {});

    render(
      <ErrorBoundary>
        <ThrowingChild />
      </ErrorBoundary>,
    );

    expect(
      screen.getByRole('heading', { name: /something went wrong/i }),
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /reload/i })).toBeInTheDocument();

    consoleSpy.mockRestore();
  });

  it('surfaces the error message in technical details', () => {
    const consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => {});

    render(
      <ErrorBoundary>
        <ThrowingChild />
      </ErrorBoundary>,
    );

    // The <details> block holds the operator-readable error text;
    // pinning prevents a future refactor that hides the cause and
    // makes debugging harder for the operator + the on-call user.
    expect(screen.getByText(/boom — synthetic test failure/)).toBeInTheDocument();

    consoleSpy.mockRestore();
  });

  it('logs the error via console.error for operator visibility', () => {
    const consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => {});

    render(
      <ErrorBoundary>
        <ThrowingChild />
      </ErrorBoundary>,
    );

    // At least one console.error call must include the ErrorBoundary
    // prefix. Other React-internal console.error calls during the
    // throw aren't our concern.
    const matchingCalls = consoleSpy.mock.calls.filter(
      (args) => typeof args[0] === 'string' && args[0].includes('[ErrorBoundary]'),
    );
    expect(matchingCalls.length).toBeGreaterThan(0);

    consoleSpy.mockRestore();
  });
});
