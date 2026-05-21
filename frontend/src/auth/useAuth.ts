/**
 * `useAuth` hook — read JWT state + login/logout actions from context.
 *
 * Split out from `AuthContext.tsx` so the provider file only exports
 * components (Fast Refresh requirement). Hook lives alongside the
 * Context object in this dir so consumers import a single thing from
 * one location: `import { useAuth } from '@/auth/useAuth'`.
 */

import { useContext } from 'react';

import { AuthContext, type AuthState } from './context';

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (ctx === null) {
    throw new Error('useAuth: must be wrapped in <AuthProvider>');
  }
  return ctx;
}
