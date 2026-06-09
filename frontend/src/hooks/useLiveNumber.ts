/**
 * `useLiveNumber` — eased count-up toward a target value (R0).
 *
 * Ported from the prototype's `useLiveNumber`. Each `target` change animates
 * the displayed number toward the new value via a geometric approach (12% of
 * the remaining distance per frame), so KPI tiles tick rather than jump.
 *
 * Two improvements over the prototype, within the "defect / mechanism" port
 * latitude (never a restyle):
 *   - **Motion gate is the OS setting** (`prefers-reduced-motion: reduce`),
 *     not a `document.body.dataset.motion` tweak attribute (D4 — motion is
 *     driven by the OS, no in-app density/motion panel ships). Under reduced
 *     motion the value snaps instantly.
 *   - **The rAF loop stops once settled** instead of rescheduling forever.
 *     The prototype kept requesting frames after reaching the target; here a
 *     settled animation cancels itself, and a near-zero target settles via an
 *     absolute epsilon (the prototype's `|target| * step` threshold is 0 at
 *     target 0, which never converges).
 *
 * No `Math.random`, no fake data — this only eases a real number you pass in.
 */

import { useEffect, useRef, useState } from 'react';

/** Fraction of the remaining distance consumed each frame. */
const APPROACH = 0.12;
/** Default relative snap threshold (snap to target when within `step * |target|`). */
const DEFAULT_STEP = 0.002;
/** Absolute snap floor so a target at/near 0 still settles. */
const MIN_EPSILON = 1e-6;

function prefersReducedMotion(): boolean {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return false;
  return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

export interface UseLiveNumberOptions {
  /** When false (or under reduced motion), the value snaps to `target` instantly. */
  live?: boolean;
  /** Relative snap threshold; smaller = animates closer before snapping. */
  step?: number;
}

/**
 * Returns a number that eases toward `target` whenever `target` changes.
 *
 * @param target  the real value to display (already computed — this hook only animates it)
 * @param options `{ live, step }` — disable animation or tune the snap threshold
 */
export function useLiveNumber(target: number, options: UseLiveNumberOptions = {}): number {
  const { live = true, step = DEFAULT_STEP } = options;
  // Whether to animate at all. When false we return `target` DIRECTLY (below)
  // rather than snapping via setState in the effect — that keeps the effect
  // free of synchronous state writes (react-hooks/set-state-in-effect).
  const animating = live && !prefersReducedMotion();

  const [value, setValue] = useState(target);
  // Holds the animated position across renders so the next frame reads it
  // without making the state updater impure.
  const valueRef = useRef(target);
  const rafRef = useRef<number | null>(null);

  useEffect(() => {
    if (!animating) {
      // Keep the ref aligned so a later re-enable eases from `target`, not a
      // stale position. No setState here — the snap happens via the return.
      valueRef.current = target;
      return;
    }

    const snapWithin = Math.max(Math.abs(target) * step, MIN_EPSILON);
    const tick = () => {
      const prev = valueRef.current;
      const delta = target - prev;
      if (Math.abs(delta) < snapWithin) {
        valueRef.current = target;
        setValue(target);
        rafRef.current = null; // settled — stop the loop
        return;
      }
      const next = prev + delta * APPROACH;
      valueRef.current = next;
      setValue(next);
      rafRef.current = requestAnimationFrame(tick);
    };

    rafRef.current = requestAnimationFrame(tick);
    return () => {
      if (rafRef.current != null) {
        cancelAnimationFrame(rafRef.current);
        rafRef.current = null;
      }
    };
  }, [target, animating, step]);

  return animating ? value : target;
}
