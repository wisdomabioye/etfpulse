/**
 * Auth Context object + type — split out from the provider component
 * file so React Fast Refresh can re-render the provider in isolation.
 *
 * Lint rule `react-refresh/only-export-components` rejects mixing
 * component exports with non-component exports in the same file —
 * the standard fix is to move the Context object + types into a
 * sibling module the component imports from.
 */

import { createContext } from 'react';

export interface AuthState {
  /** Current JWT, or `null` when logged out. */
  jwt: string | null;
  /** Convenience boolean. */
  isAuthed: boolean;
  /** Store a freshly-minted token. Called by the SIWE login flow + D.5. */
  login: (jwt: string) => void;
  /** Clear the token. Called on user-initiated logout + 401 interceptor. */
  logout: () => void;
}

// `null` initial means consumers MUST be wrapped in <AuthProvider>;
// `useAuth` throws on the null path so the missing-wrap case fails at
// render rather than silently returning undefined fields.
export const AuthContext = createContext<AuthState | null>(null);
