/**
 * `<AuthProvider>` — mirrors the JWT storage layer into React state +
 * wires up the 401 interceptor on the API client.
 *
 *   - Hydrates from sessionStorage on first render.
 *   - Subscribes to storage updates so other code paths (e.g., D.5
 *     Telegram WebApp auto-login) that call `setJwt` reflect
 *     immediately in the React tree.
 *   - Registers `setOnUnauthorized` so any 401 from the API client
 *     triggers `setJwt(null)` + `navigate('/login')`.
 *
 * The Context object + `useAuth` hook live in sibling files
 * (`context.ts`, `useAuth.ts`) so React Fast Refresh can re-render
 * this provider in isolation per the
 * `react-refresh/only-export-components` rule.
 */

import { type ReactNode, useCallback, useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';

import { setOnUnauthorized } from '../api/client';
import { AuthContext, type AuthState } from './context';
import { getJwt, setJwt, subscribeJwt } from './storage';

export function AuthProvider({ children }: { children: ReactNode }) {
  const navigate = useNavigate();
  // Initialise from storage on first render. `getJwt` is idempotent and
  // cheap; safe to call during render to avoid a hydration flicker.
  const [jwt, setJwtState] = useState<string | null>(() => getJwt());

  // Mirror storage→React. Any other code that calls `setJwt(...)`
  // (the SIWE login flow, the 401 interceptor, the D.5 WebApp path)
  // wins on this subscription.
  useEffect(() => {
    return subscribeJwt((next) => setJwtState(next));
  }, []);

  // 401 interceptor — clears storage + redirects to /login. We attach
  // here (not at module load) so navigation has the React Router
  // context available.
  useEffect(() => {
    setOnUnauthorized(() => {
      setJwt(null);
      navigate('/login', { replace: true });
    });
    return () => setOnUnauthorized(null);
  }, [navigate]);

  const login = useCallback((next: string) => setJwt(next), []);
  const logout = useCallback(() => {
    setJwt(null);
    navigate('/login', { replace: true });
  }, [navigate]);

  const value: AuthState = {
    jwt,
    isAuthed: jwt !== null,
    login,
    logout,
  };
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
