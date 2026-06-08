/**
 * Post-login redirect resolver (SIG2X.3).
 *
 * Pure helper extracted from `pages/Login.tsx` so the Login file
 * exports only React components (the `react-refresh/only-export-components`
 * lint rule that's bit us twice before).
 *
 * Reads `location.state.from` set by `<Navigate state={{from: location}}>`
 * upstream and composes the full URL string (`pathname + search + hash`).
 * Falls back to `/execute` when no origin was recorded (direct visit
 * to /login).
 */

import type { Location } from 'react-router-dom';

export function resolvePostLoginPath(location: Location): string {
  const state = location.state as { from?: Location } | null;
  const from = state?.from;
  if (!from) return '/execute';
  return `${from.pathname}${from.search ?? ''}${from.hash ?? ''}`;
}
