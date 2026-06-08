/**
 * Admin key storage helpers — shared between `/admin` and
 * `/admin/backtest`. SessionStorage holds the key for the tab's
 * lifetime; it never persists to disk.
 *
 * The try/catch tolerates `SecurityError` (private mode / disabled
 * storage). Pages degrade gracefully to in-memory state — the key
 * still works for the current render, just won't survive a reload.
 */

const SESSION_KEY = 'etfpulse:admin_key';

export function loadAdminKey(): string {
  try {
    return sessionStorage.getItem(SESSION_KEY) ?? '';
  } catch {
    return '';
  }
}

export function saveAdminKey(key: string) {
  try {
    if (key) sessionStorage.setItem(SESSION_KEY, key);
    else sessionStorage.removeItem(SESSION_KEY);
  } catch {
    // ignore — private mode / disabled storage; in-memory state still works.
  }
}
