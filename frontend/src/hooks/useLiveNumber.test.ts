/**
 * Tests for `useLiveNumber` — the motion-gated count-up hook (R0).
 *
 * `requestAnimationFrame` and `matchMedia` aren't reliably present/controllable
 * under jsdom, so both are stubbed: rAF callbacks are queued and flushed
 * frame-by-frame under `act()`, and matchMedia is stubbed per-test to toggle the
 * reduced-motion gate. Covers: eased approach, exact settle, live=false snap,
 * reduced-motion snap, large-step early settle, and unmount cancellation.
 */

import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { useLiveNumber } from './useLiveNumber';

let rafCbs: FrameRequestCallback[] = [];
let cancelSpy: ReturnType<typeof vi.fn>;

/** Stub matchMedia so `(prefers-reduced-motion: reduce)` returns `matches`. */
function stubMatchMedia(matches: boolean) {
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches,
    media: query,
    addEventListener: () => {},
    removeEventListener: () => {},
  }));
}

/** Run `n` animation frames (each queued callback may enqueue the next). */
function flushFrames(n: number) {
  for (let i = 0; i < n; i++) {
    const cbs = rafCbs;
    rafCbs = [];
    if (cbs.length === 0) break;
    act(() => {
      cbs.forEach((cb) => cb(0));
    });
  }
}

beforeEach(() => {
  rafCbs = [];
  cancelSpy = vi.fn();
  vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => {
    rafCbs.push(cb);
    return rafCbs.length;
  });
  vi.stubGlobal('cancelAnimationFrame', cancelSpy);
  stubMatchMedia(false); // motion on by default
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('useLiveNumber', () => {
  it('holds the initial target and settles the mount frame to a no-op', () => {
    const { result } = renderHook(({ target }) => useLiveNumber(target), {
      initialProps: { target: 42 },
    });
    expect(result.current).toBe(42);
    // The mount effect queues exactly one frame; with delta 0 it settles on
    // run and does NOT reschedule.
    expect(rafCbs).toHaveLength(1);
    flushFrames(5);
    expect(result.current).toBe(42);
    expect(rafCbs).toHaveLength(0);
  });

  it('eases toward a changed target then settles exactly', () => {
    const { result, rerender } = renderHook(({ target }) => useLiveNumber(target), {
      initialProps: { target: 0 },
    });
    rerender({ target: 100 });

    flushFrames(1);
    // First frame moves 12% of the distance: 0 + 100*0.12 = 12.
    expect(result.current).toBeCloseTo(12, 5);
    expect(result.current).toBeGreaterThan(0);
    expect(result.current).toBeLessThan(100);

    flushFrames(200);
    expect(result.current).toBe(100); // exact snap, not 99.9…
  });

  it('snaps instantly when live is false', () => {
    const { result, rerender } = renderHook(
      ({ target, live }) => useLiveNumber(target, { live }),
      { initialProps: { target: 0, live: false } },
    );
    rerender({ target: 100, live: false });
    expect(result.current).toBe(100); // no frames flushed
    expect(rafCbs).toHaveLength(0);
  });

  it('snaps instantly under prefers-reduced-motion', () => {
    stubMatchMedia(true);
    const { result, rerender } = renderHook(({ target }) => useLiveNumber(target), {
      initialProps: { target: 0 },
    });
    rerender({ target: 100 });
    expect(result.current).toBe(100);
    expect(rafCbs).toHaveLength(0);
  });

  it('settles on the first frame when step is large enough', () => {
    const { result, rerender } = renderHook(
      ({ target, step }) => useLiveNumber(target, { step }),
      { initialProps: { target: 0, step: 2 } },
    );
    // snapWithin = |100| * 2 = 200 > 100 → first tick settles.
    rerender({ target: 100, step: 2 });
    flushFrames(1);
    expect(result.current).toBe(100);
  });

  it('cancels the pending frame on unmount mid-animation', () => {
    const { rerender, unmount } = renderHook(({ target }) => useLiveNumber(target), {
      initialProps: { target: 0 },
    });
    rerender({ target: 100 });
    flushFrames(1); // mid-flight, a frame is queued
    unmount();
    expect(cancelSpy).toHaveBeenCalled();
  });
});
