/**
 * Backtest page tests (#206).
 *
 * Coverage targets:
 *   - Page renders gate when no admin key is active.
 *   - Submitting the gate unlocks the form, which fetches detectors.
 *   - Form submit posts the right body shape + `X-Admin-Key` header.
 *   - `allow_ai` toggle defaults off; the warning copy is visible.
 *   - When `allow_ai=true`, the request includes the
 *     `X-Backtest-Allow-AI: yes` header.
 *   - Results table renders one row per detector returned by the
 *     orchestrator, with the hit_rate cell formatted as a percent.
 *   - Mutation error renders a Callout instead of the results card.
 *
 * Mock strategy: stub `globalThis.fetch` so the page's TanStack Query
 * hooks see canned responses without going near the real backend.
 * Mirrors the pattern in `pages/Admin.test.tsx`.
 */

import type { ReactNode } from 'react';

import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { BacktestPage } from './Backtest';

const ADMIN_KEY = 'test-admin-key';

const DETECTORS_RESPONSE = {
  detectors: [
    {
      name: 'flow_anomaly',
      signal_type: 'flow_anomaly',
      params: [
        { name: 'lookback_days', type_name: 'int', has_default: true, default: 14 },
        {
          name: 'min_streak_length',
          type_name: 'int',
          has_default: true,
          default: 3,
        },
      ],
    },
    {
      name: 'magnitude',
      signal_type: 'magnitude',
      params: [
        {
          name: 'percentile_threshold',
          type_name: 'float',
          has_default: true,
          default: 0.8,
        },
      ],
    },
  ],
};

const REPORT_RESPONSE = {
  start: '2026-04-01',
  end: '2026-04-07',
  ai_prompt_version: 'v3',
  detector_configs: {},
  counters: { hits_total: 5, scored_total: 4 },
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
      n_hits: 2,
      n_scored: 1,
      wins: 0,
      losses: 1,
      hit_rate: 0.0,
    },
  ],
  outcomes: [],
};

interface CapturedRequest {
  url: string;
  method: string;
  body: string | undefined;
  headers: Record<string, string>;
}

function stubFetch(
  captured: CapturedRequest[],
  responder: (req: CapturedRequest) => { status?: number; body: unknown },
) {
  return vi.spyOn(globalThis, 'fetch').mockImplementation((url, init) => {
    const req: CapturedRequest = {
      url: String(url),
      method: (init?.method ?? 'GET').toString(),
      body: typeof init?.body === 'string' ? init.body : undefined,
      headers: { ...((init?.headers as Record<string, string>) ?? {}) },
    };
    captured.push(req);
    const r = responder(req);
    return Promise.resolve(
      new Response(JSON.stringify(r.body), {
        status: r.status ?? 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    );
  });
}

function renderPage(ui: ReactNode = <BacktestPage />) {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0 },
      mutations: { retry: false },
    },
  });
  return render(
    <MemoryRouter initialEntries={['/admin/backtest']}>
      <QueryClientProvider client={client}>{ui}</QueryClientProvider>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  sessionStorage.clear();
});

afterEach(() => {
  vi.restoreAllMocks();
  // Belt-and-braces: any test that swapped in fake timers and threw
  // before its own cleanup must not leak fake timers into the next
  // test file. `restoreAllMocks` doesn't reset timers — that's the
  // missing piece.
  vi.useRealTimers();
  sessionStorage.clear();
});

describe('BacktestPage gate', () => {
  it('renders the empty state when no admin key is active', () => {
    renderPage();
    expect(
      screen.getByText(/enter your admin key to run a backtest/i),
    ).toBeInTheDocument();
    expect(screen.queryByText(/detector overrides/i)).not.toBeInTheDocument();
  });

  it('unlocks the form when a key is submitted', async () => {
    const calls: CapturedRequest[] = [];
    stubFetch(calls, () => ({ body: DETECTORS_RESPONSE }));

    renderPage();
    fireEvent.change(screen.getByPlaceholderText(/X-Admin-Key/i), {
      target: { value: ADMIN_KEY },
    });
    fireEvent.click(screen.getByRole('button', { name: /unlock/i }));

    await waitFor(() => {
      expect(screen.getByText(/detector overrides/i)).toBeInTheDocument();
    });
    // Detectors endpoint called with the admin key header.
    const detectorsCall = calls.find((c) =>
      c.url.includes('/api/admin/backtest/detectors'),
    );
    expect(detectorsCall).toBeDefined();
    expect(detectorsCall!.headers['X-Admin-Key']).toBe(ADMIN_KEY);
  });
});

describe('BacktestPage form', () => {
  function unlockedRender() {
    sessionStorage.setItem('etfpulse:admin_key', ADMIN_KEY);
    return renderPage();
  }

  it('renders allow_ai toggle off by default with the warning copy', async () => {
    const calls: CapturedRequest[] = [];
    stubFetch(calls, () => ({ body: DETECTORS_RESPONSE }));
    unlockedRender();
    await waitFor(() => {
      expect(screen.getByText(/allow live AI calls/i)).toBeInTheDocument();
    });
    const checkbox = screen.getByRole('checkbox');
    expect(checkbox).not.toBeChecked();
    expect(screen.getByText(/charged against the OpenRouter daily cap/i)).toBeInTheDocument();
  });

  it('submits a request body with start, end, and admin key', async () => {
    const calls: CapturedRequest[] = [];
    stubFetch(calls, (req) => {
      if (req.url.endsWith('/api/admin/backtest/detectors')) {
        return { body: DETECTORS_RESPONSE };
      }
      return { body: REPORT_RESPONSE };
    });
    unlockedRender();
    await waitFor(() =>
      expect(screen.getByText(/flow_anomaly/i)).toBeInTheDocument(),
    );

    fireEvent.click(screen.getByRole('button', { name: /run backtest/i }));

    await waitFor(() => {
      const post = calls.find(
        (c) => c.method === 'POST' && c.url.endsWith('/api/admin/backtest'),
      );
      expect(post).toBeDefined();
    });
    const post = calls.find(
      (c) => c.method === 'POST' && c.url.endsWith('/api/admin/backtest'),
    )!;
    const body = JSON.parse(post.body ?? '{}');
    expect(typeof body.start).toBe('string');
    expect(typeof body.end).toBe('string');
    expect(post.headers['X-Admin-Key']).toBe(ADMIN_KEY);
    // No allow_ai flag → header MUST be absent on this path.
    expect(post.headers['X-Backtest-Allow-AI']).toBeUndefined();
  });

  it('sends the X-Backtest-Allow-AI header when allow_ai is checked', async () => {
    const calls: CapturedRequest[] = [];
    stubFetch(calls, (req) => {
      if (req.url.endsWith('/api/admin/backtest/detectors')) {
        return { body: DETECTORS_RESPONSE };
      }
      return { body: REPORT_RESPONSE };
    });
    unlockedRender();
    await waitFor(() =>
      expect(screen.getByText(/flow_anomaly/i)).toBeInTheDocument(),
    );

    fireEvent.click(screen.getByRole('checkbox'));
    fireEvent.click(screen.getByRole('button', { name: /run backtest/i }));

    await waitFor(() => {
      const post = calls.find(
        (c) => c.method === 'POST' && c.url.endsWith('/api/admin/backtest'),
      );
      expect(post).toBeDefined();
    });
    const post = calls.find(
      (c) => c.method === 'POST' && c.url.endsWith('/api/admin/backtest'),
    )!;
    expect(post.headers['X-Backtest-Allow-AI']).toBe('yes');
    expect(JSON.parse(post.body ?? '{}').allow_ai).toBe(true);
  });
});

describe('BacktestPage override coercion', () => {
  function unlockedRender() {
    sessionStorage.setItem('etfpulse:admin_key', ADMIN_KEY);
    return renderPage();
  }

  it('coerces a typed int override into the submitted body', async () => {
    const calls: CapturedRequest[] = [];
    stubFetch(calls, (req) => {
      if (req.url.endsWith('/api/admin/backtest/detectors')) {
        return { body: DETECTORS_RESPONSE };
      }
      return { body: REPORT_RESPONSE };
    });
    unlockedRender();
    await waitFor(() =>
      expect(screen.getByText(/flow_anomaly/i)).toBeInTheDocument(),
    );

    const inputs = screen.getAllByPlaceholderText('14');
    fireEvent.change(inputs[0], { target: { value: '21' } });
    fireEvent.click(screen.getByRole('button', { name: /run backtest/i }));

    await waitFor(() => {
      const post = calls.find(
        (c) => c.method === 'POST' && c.url.endsWith('/api/admin/backtest'),
      );
      expect(post).toBeDefined();
    });
    const post = calls.find(
      (c) => c.method === 'POST' && c.url.endsWith('/api/admin/backtest'),
    )!;
    const body = JSON.parse(post.body ?? '{}');
    expect(body.detector_overrides).toEqual({
      flow_anomaly: { lookback_days: 21 },
    });
  });
});

describe('BacktestPage stale-results guard', () => {
  it('hides the old results card while a new run is in flight', async () => {
    sessionStorage.setItem('etfpulse:admin_key', ADMIN_KEY);
    const calls: CapturedRequest[] = [];
    // First POST resolves immediately, second is held pending so we
    // can observe the in-flight render state.
    let postCounter = 0;
    let resolveSecond!: (value: Response) => void;
    const secondPostResponse = new Promise<Response>((resolve) => {
      resolveSecond = resolve;
    });

    vi.spyOn(globalThis, 'fetch').mockImplementation((url, init) => {
      const u = String(url);
      calls.push({
        url: u,
        method: (init?.method ?? 'GET').toString(),
        body: typeof init?.body === 'string' ? init.body : undefined,
        headers: { ...((init?.headers as Record<string, string>) ?? {}) },
      });
      if (u.endsWith('/api/admin/backtest/detectors')) {
        return Promise.resolve(
          new Response(JSON.stringify(DETECTORS_RESPONSE), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          }),
        );
      }
      // POST
      postCounter += 1;
      if (postCounter === 1) {
        return Promise.resolve(
          new Response(JSON.stringify(REPORT_RESPONSE), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          }),
        );
      }
      return secondPostResponse;
    });

    renderPage();
    await waitFor(() =>
      expect(screen.getByText(/flow_anomaly/i)).toBeInTheDocument(),
    );

    // First run — results card renders with the report.
    fireEvent.click(screen.getByRole('button', { name: /run backtest/i }));
    await waitFor(() => {
      expect(screen.getByRole('table')).toBeInTheDocument();
    });

    // Second run — kicks off a new mutation. The old results card MUST
    // disappear immediately so the operator doesn't read stale numbers
    // next to the running timer.
    fireEvent.click(screen.getByRole('button', { name: /run backtest/i }));
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /running…/i })).toBeDisabled();
    });
    expect(screen.queryByRole('table')).not.toBeInTheDocument();

    // Resolve so React Query can finish — keeps the test runner clean.
    act(() => {
      resolveSecond(
        new Response(JSON.stringify(REPORT_RESPONSE), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      );
    });
  });
});

describe('BacktestPage double-submit guard', () => {
  it('ignores a second Enter-key submit while a backtest is in flight', async () => {
    sessionStorage.setItem('etfpulse:admin_key', ADMIN_KEY);
    const calls: CapturedRequest[] = [];
    let resolvePost!: (value: Response) => void;
    const pendingPostResponse = new Promise<Response>((resolve) => {
      resolvePost = resolve;
    });

    vi.spyOn(globalThis, 'fetch').mockImplementation((url, init) => {
      const u = String(url);
      const req: CapturedRequest = {
        url: u,
        method: (init?.method ?? 'GET').toString(),
        body: typeof init?.body === 'string' ? init.body : undefined,
        headers: { ...((init?.headers as Record<string, string>) ?? {}) },
      };
      calls.push(req);
      if (u.endsWith('/api/admin/backtest/detectors')) {
        return Promise.resolve(
          new Response(JSON.stringify(DETECTORS_RESPONSE), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          }),
        );
      }
      return pendingPostResponse;
    });

    renderPage();
    await waitFor(() =>
      expect(screen.getByText(/flow_anomaly/i)).toBeInTheDocument(),
    );

    // First submit — kicks off the mutation; the post promise stays pending.
    fireEvent.click(screen.getByRole('button', { name: /run backtest/i }));

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /running…/i })).toBeDisabled();
    });

    // Simulate the Enter-key double-submit path. We dispatch a form
    // submit event directly — the form's onSubmit handler runs even
    // when the button is disabled (browsers don't gate form submission
    // on submit-button disabled state). With the busy-guard in place
    // the second fetch never reaches the network.
    const form = screen.getByRole('button', { name: /running…/i }).closest('form');
    expect(form).not.toBeNull();
    fireEvent.submit(form!);

    // Drain microtasks; if the guard is broken a second POST would be
    // queued. We assert only one POST recorded.
    await Promise.resolve();
    const postCount = calls.filter(
      (c) => c.method === 'POST' && c.url.endsWith('/api/admin/backtest'),
    ).length;
    expect(postCount).toBe(1);

    // Cleanup — resolve the pending post so React Query can finish.
    act(() => {
      resolvePost(
        new Response(JSON.stringify(REPORT_RESPONSE), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      );
    });
  });
});

describe('BacktestPage clear key flow', () => {
  it('returns to the locked state when the key is cleared', async () => {
    sessionStorage.setItem('etfpulse:admin_key', ADMIN_KEY);
    const calls: CapturedRequest[] = [];
    stubFetch(calls, () => ({ body: DETECTORS_RESPONSE }));
    renderPage();

    await waitFor(() =>
      expect(screen.getByText(/detector overrides/i)).toBeInTheDocument(),
    );

    fireEvent.click(screen.getByRole('button', { name: /clear key/i }));

    await waitFor(() => {
      expect(
        screen.getByText(/enter your admin key to run a backtest/i),
      ).toBeInTheDocument();
    });
    expect(sessionStorage.getItem('etfpulse:admin_key')).toBeNull();
  });
});

describe('BacktestPage pending state', () => {
  function unlockedRender() {
    sessionStorage.setItem('etfpulse:admin_key', ADMIN_KEY);
    return renderPage();
  }

  it('renders the elapsed timer while the mutation is in flight', async () => {
    vi.useFakeTimers();
    let resolvePost!: (value: Response) => void;
    const pendingPostResponse = new Promise<Response>((resolve) => {
      resolvePost = resolve;
    });

    vi.spyOn(globalThis, 'fetch').mockImplementation((url, init) => {
      const u = String(url);
      if (u.endsWith('/api/admin/backtest/detectors')) {
        return Promise.resolve(
          new Response(JSON.stringify(DETECTORS_RESPONSE), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          }),
        );
      }
      if ((init?.method ?? 'GET') === 'POST') {
        return pendingPostResponse;
      }
      return Promise.resolve(new Response('{}', { status: 200 }));
    });

    unlockedRender();
    await vi.waitFor(() =>
      expect(screen.getByText(/flow_anomaly/i)).toBeInTheDocument(),
    );

    fireEvent.click(screen.getByRole('button', { name: /run backtest/i }));

    await vi.waitFor(() => {
      expect(screen.getByRole('button', { name: /running…/i })).toBeDisabled();
    });

    // Advance fake timers by 2s — the elapsed counter must tick.
    act(() => {
      vi.advanceTimersByTime(2000);
    });
    expect(screen.getByText(/2s elapsed/i)).toBeInTheDocument();

    // Let the mutation resolve so React Query can unmount the timer.
    act(() => {
      resolvePost(
        new Response(JSON.stringify(REPORT_RESPONSE), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      );
    });
    vi.useRealTimers();
  });
});

describe('BacktestPage results', () => {
  function unlockedRender() {
    sessionStorage.setItem('etfpulse:admin_key', ADMIN_KEY);
    return renderPage();
  }

  it('renders one row per detector in the report', async () => {
    const calls: CapturedRequest[] = [];
    stubFetch(calls, (req) => {
      if (req.url.endsWith('/api/admin/backtest/detectors')) {
        return { body: DETECTORS_RESPONSE };
      }
      return { body: REPORT_RESPONSE };
    });
    unlockedRender();
    await waitFor(() =>
      expect(screen.getByText(/flow_anomaly/i)).toBeInTheDocument(),
    );

    fireEvent.click(screen.getByRole('button', { name: /run backtest/i }));

    await waitFor(() => {
      expect(screen.getByText(/results/i)).toBeInTheDocument();
    });
    const table = screen.getByRole('table');
    const rows = within(table).getAllByRole('row');
    // 1 header row + 2 detector rows.
    expect(rows).toHaveLength(3);
    // hit_rate formatted as percent.
    expect(within(table).getByText('66.7%')).toBeInTheDocument();
  });

  it('renders a Callout on mutation error instead of the results card', async () => {
    const calls: CapturedRequest[] = [];
    stubFetch(calls, (req) => {
      if (req.url.endsWith('/api/admin/backtest/detectors')) {
        return { body: DETECTORS_RESPONSE };
      }
      return {
        status: 422,
        body: { detail: 'window 8d exceeds cap 3d; tighten the date range' },
      };
    });
    unlockedRender();
    await waitFor(() =>
      expect(screen.getByText(/flow_anomaly/i)).toBeInTheDocument(),
    );

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /run backtest/i }));
    });

    await waitFor(() => {
      expect(screen.getByText(/exceeds cap/i)).toBeInTheDocument();
    });
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
  });
});
