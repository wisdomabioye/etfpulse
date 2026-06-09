/**
 * Admin operations TanStack Query hooks — gated by `X-Admin-Key`.
 *
 * Per-user mutations (paper-trade flag, wallet unbind), execution circuit
 * breaker (halt / resume), and SoDEX symbols refresh. See `queries-admin`
 * for metrics, pipeline triggers, delivery trace, and webhook rotation.
 */

import { useMutation, useQueryClient } from '@tanstack/react-query';
import { apiPost } from './client';

/** Response shape of `POST /api/admin/users/{id}/paper-trade`.
 *  Mirrors backend `SetPaperTradeResponse`. */
export interface SetPaperTradeResult {
  user_id: number;
  paper_trade: boolean;
}

/** Operator flips a user's paper-trade flag. Idempotent at the API level
 *  (sending the current value succeeds with no state change). Backend
 *  acquires SELECT FOR UPDATE on the User row so this serialises with
 *  any concurrent prepare path. Cooldown / dedupe handled by the caller
 *  if needed; no internal retry (operator decides). #186. */
export function useSetUserPaperTrade(adminKey: string) {
  // No `useQueryClient` — there's no admin query that reflects per-user
  // paper_trade state. The affected user's wallet/me query refreshes
  // via its own 30s poll. Nothing to invalidate here.
  return useMutation({
    mutationFn: ({ userId, paperTrade }: { userId: number; paperTrade: boolean }) =>
      apiPost<SetPaperTradeResult>(
        `/api/admin/users/${userId}/paper-trade`,
        { paper_trade: paperTrade },
        { 'X-Admin-Key': adminKey },
      ),
    retry: false,
  });
}

/** Response shape of `POST /api/admin/users/{id}/unbind-wallet`.
 *  Mirrors backend `UnbindWalletResponse`. */
export interface UnbindWalletResult {
  user_id: number;
  was_bound: boolean;
  previous_wallet_address: string | null;
}

/** Operator clears a user's wallet binding. Clears wallet_address +
 *  sodex_account_id + both api_key_name fields atomically (rule 30
 *  serialises with concurrent prepares). Idempotent: re-running an
 *  already-unbound user returns `was_bound=false`. Destructive — UI
 *  MUST confirm. #186. */
export function useUnbindUserWallet(adminKey: string) {
  return useMutation({
    mutationFn: (userId: number) =>
      apiPost<UnbindWalletResult>(
        `/api/admin/users/${userId}/unbind-wallet`,
        undefined,
        { 'X-Admin-Key': adminKey },
      ),
    retry: false,
  });
}

/** Response shape of `POST /api/admin/execution/halt`. Mirrors backend
 *  `HaltExecutionResponse`. `existing_details` is the operator-supplied
 *  context from a prior halt (when `already_active=true`). */
export interface HaltExecutionResult {
  breaker_id: number;
  scope: string;
  already_active: boolean;
  existing_triggered_at: string | null;
  existing_details: Record<string, unknown> | null;
}

/** Trip the `manual` circuit breaker — global (user_id null) or
 *  per-user. Future prepare calls in the scope return 503 until a
 *  matching resume. Idempotent: re-halting an active scope returns
 *  `already_active=true` with the existing breaker details, no
 *  duplicate row. Destructive — UI MUST confirm. #186. */
export function useHaltExecution(adminKey: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ reason, userId }: { reason: string; userId: number | null }) =>
      apiPost<HaltExecutionResult>(
        '/api/admin/execution/halt',
        { reason, user_id: userId },
        { 'X-Admin-Key': adminKey },
      ),
    retry: false,
    onSuccess: () => {
      // Breaker count visible on the metrics dashboard.
      qc.invalidateQueries({ queryKey: ['admin', 'metrics'] });
    },
  });
}

/** Response shape of `POST /api/admin/execution/resume`. Mirrors backend
 *  `ResumeExecutionResponse`. */
export interface ResumeExecutionResult {
  rowcount: number;
  scope: string;
}

/** Resolve the `manual` circuit breaker for a scope. Global resume does
 *  NOT clear per-user breakers (independent dimensions) — operator
 *  must call once per scope. Idempotent: nothing-to-resume returns
 *  rowcount=0 with 200 (not an error). #186. */
export function useResumeExecution(adminKey: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (userId: number | null) =>
      apiPost<ResumeExecutionResult>(
        '/api/admin/execution/resume',
        { user_id: userId },
        { 'X-Admin-Key': adminKey },
      ),
    retry: false,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['admin', 'metrics'] });
    },
  });
}

/** Response shape of `POST /api/admin/sodex/symbols/refresh`. Mirrors
 *  backend `SymbolsRefreshResponse`. */
export interface SymbolsRefreshResult {
  spot_inserted: number;
  spot_updated: number;
  perps_inserted: number;
  perps_updated: number;
  errors: number;
  spot_parse_errors?: number;
  perps_parse_errors?: number;
}

/** Force a `sodex_symbols` refresh. Daily cron handles steady-state;
 *  manual refresh covers new-listing surprises (operator hears about
 *  a new pair, doesn't want to wait until 04:00 UTC). Backend returns
 *  503 when SoDEX HTTP clients aren't attached (scheduler disabled).
 *  #186. */
export function useRefreshSodexSymbols(adminKey: string) {
  return useMutation({
    mutationFn: () =>
      apiPost<SymbolsRefreshResult>(
        '/api/admin/sodex/symbols/refresh',
        undefined,
        { 'X-Admin-Key': adminKey },
      ),
    retry: false,
  });
}
