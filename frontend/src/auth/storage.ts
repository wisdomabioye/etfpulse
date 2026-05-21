/**
 * JWT persistence layer.
 *
 * Storage choice: `sessionStorage`, NOT `localStorage`.
 *   - sessionStorage is scoped per-tab and cleared on tab close.
 *   - localStorage persists across browser restarts AND is shared
 *     between tabs of the same origin — a stolen token via XSS
 *     persists forever, and one compromised tab leaks to all others.
 *   - For a finance/execution surface, per-tab isolation is the
 *     conservative default. Users sign in per tab; closing the tab is
 *     a logout. Re-launching the Telegram WebApp (D.5) re-mints fresh.
 *
 * Module-level memory cache to avoid re-reading sessionStorage on
 * every API call (sessionStorage is sync but a getItem is still
 * non-trivial work; memoising it keeps the hot fetch path fast).
 *
 * No React coupling — this module is import-safe from the API client
 * layer, which can't depend on React Context.
 */

const STORAGE_KEY = 'etfpulse:jwt';

// Module-level cache. `undefined` = not yet loaded; `null` = no token;
// string = the active JWT. The undefined sentinel matters because
// sessionStorage access can throw in private-browsing contexts where
// 3p storage is blocked — we fall back to in-memory only.
let cachedJwt: string | null | undefined = undefined;

// Subscribers fire on every setJwt() so React state can mirror the
// storage value without polling.
type Listener = (jwt: string | null) => void;
const listeners = new Set<Listener>();

function readFromStorage(): string | null {
  try {
    return sessionStorage.getItem(STORAGE_KEY);
  } catch {
    // Private browsing / blocked storage — fall back to in-memory.
    return null;
  }
}

function writeToStorage(value: string | null): void {
  try {
    if (value === null) sessionStorage.removeItem(STORAGE_KEY);
    else sessionStorage.setItem(STORAGE_KEY, value);
  } catch {
    // Storage unavailable — in-memory only. Token is lost on reload.
  }
}

/**
 * Read the current JWT, hydrating from sessionStorage on first call.
 *
 * Returns `null` when no token is stored OR storage is unavailable.
 * Synchronous + cheap on the hot path (memory hit after first read).
 */
export function getJwt(): string | null {
  if (cachedJwt === undefined) {
    cachedJwt = readFromStorage();
  }
  return cachedJwt;
}

/**
 * Set or clear the JWT. Persists to sessionStorage and notifies
 * subscribers (the React `useAuth` hook listens here).
 *
 * Passing `null` is the explicit "log out" operation.
 */
export function setJwt(jwt: string | null): void {
  cachedJwt = jwt;
  writeToStorage(jwt);
  for (const listener of listeners) listener(jwt);
}

/**
 * Subscribe to JWT changes. Returns an unsubscribe function — React
 * `useEffect` callers MUST return it for cleanup.
 */
export function subscribeJwt(listener: Listener): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}
