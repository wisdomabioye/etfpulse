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

import { type ReactNode, useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';

import { setOnUnauthorized } from '../api/client';
import { announceWebAppReady, getInitDataRaw } from '../lib/telegram-webapp';
import { AuthContext, type AuthState } from './context';
import { getJwt, setJwt, subscribeJwt } from './storage';
import { performTelegramVerify } from './telegram';

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

  // Telegram WebApp auto-login. Single-shot per page load: if we're
  // running inside a Telegram WebApp AND have no JWT yet, POST the
  // initData to the verify route and store the returned JWT. Failure
  // is suppressed (logged once) — SIWE remains the fallback path.
  //
  // `attemptedRef` prevents the StrictMode double-invoke from firing
  // the verify call twice. Without the ref, dev mode would POST
  // initData → consume nothing (the backend's WebApp verify isn't
  // single-use), but it'd produce duplicate log entries and a brief
  // race where the second JWT could overwrite the first.
  //
  // We also call `announceWebAppReady()` regardless of auth path so
  // Telegram's loading splash dismisses even when the user is
  // already authed from a previous session.
  const attemptedRef = useRef(false);
  useEffect(() => {
    announceWebAppReady();
    if (attemptedRef.current) return;
    attemptedRef.current = true;
    if (jwt !== null) return; // already authed
    const initData = getInitDataRaw();
    if (initData === null) return; // not in a Telegram WebApp
    void (async () => {
      try {
        const resp = await performTelegramVerify(initData);
        setJwt(resp.jwt);
      } catch (e) {
        // Silent failure — SIWE path stays available. Surface to
        // console for operator diagnosis without crashing the SPA.
        console.warn('[auth] Telegram WebApp auto-login failed:', e);
      }
    })();
    // `jwt` intentionally excluded from deps — we only want to run
    // this on the FIRST mount; subsequent jwt changes (e.g., user
    // logs out) should NOT re-trigger auto-verify with the same
    // initData (the backend would re-consume + re-mint, which is
    // harmless but spammy in logs).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

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
