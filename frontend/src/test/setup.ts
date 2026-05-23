/**
 * Vitest setup — runs once per test worker before any test file.
 *
 * Extends `expect()` with `@testing-library/jest-dom` matchers
 * (`toBeInTheDocument`, `toHaveAttribute`, `toBeDisabled`, etc) so
 * tests read naturally. Without this, every test file would have to
 * import the matchers manually.
 *
 * Also wires `cleanup()` after every test — Testing Library auto-cleanup
 * is enabled in v15+ but pinning explicitly here makes it visible at
 * the setup layer (where a future failure mode would show up).
 */

import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/react';
import { afterEach } from 'vitest';

afterEach(() => {
  cleanup();
});
