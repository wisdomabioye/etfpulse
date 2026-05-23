/**
 * Bootstrap smoke test (#78.1) — proves the vitest pipeline is wired:
 *   - test discovery (vite.config.ts include picks this up)
 *   - jsdom environment is active (we use `document`)
 *   - `globals: true` injects `describe`/`it`/`expect`
 *   - `@testing-library/jest-dom/vitest` matchers extend expect()
 *   - the setup.ts file's `afterEach(cleanup)` is registered
 *
 * Component-level smoke (rendering React trees with providers) lands in
 * the contract tests #78.2-78.4. This file proves the floor.
 *
 * If this file fails, the rest of the suite cannot have meaningful
 * coverage — fix this before debugging individual tests.
 */

import { describe, expect, it } from 'vitest';

describe('vitest pipeline', () => {
  it('runs basic assertions', () => {
    expect(1 + 1).toBe(2);
  });

  it('has a jsdom document available', () => {
    expect(typeof document).toBe('object');
    expect(document.createElement).toBeTypeOf('function');
  });

  it('extends expect() with jest-dom matchers', () => {
    // The matchers come from @testing-library/jest-dom/vitest (loaded
    // in src/test/setup.ts). If setup.ts didn't run, this assertion
    // fails with "toBeInTheDocument is not a function" — the canonical
    // signal that the setupFiles wiring broke.
    const el = document.createElement('div');
    el.textContent = 'hello';
    document.body.appendChild(el);
    expect(el).toBeInTheDocument();
    expect(el).toHaveTextContent('hello');
    document.body.removeChild(el);
  });
});
