/**
 * TanStack Query hooks for the execution surface.
 *
 * Polling cadence:
 *   - useOrders: 5s — open-order state changes fast (ack, fill, cancel).
 *   - usePositions: 15s — positions move on fill, not by themselves.
 *   - useSymbols: cached for the session (refetch on demand only).
 *   - useWalletMe: 30s — api_key_name / paper_trade can flip via admin.
 *
 * Mutations invalidate the relevant query keys so the UI snaps to the
 * post-mutation state without a poll wait. The keys are conservatively
 * coarse (no per-filter sub-keys for the lists) — fine for the typical
 * single-user-single-tab workload.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import {
  type PrepareCancelResponse,
  type PrepareNewRequest,
  type PrepareNewResponse,
  type RequestLiveRequest,
  type RequestLiveResponse,
  type SetApiKeyRequest,
  type SubmitResponse,
  type Venue,
  fetchOrders,
  fetchPositions,
  fetchSodexBootstrap,
  fetchSymbols,
  fetchWalletMe,
  postClosePosition,
  postPrepareCancel,
  postPrepareNew,
  postRequestLive,
  postSubmitCancel,
  postSubmitNew,
  postWalletApiKey,
} from '../api/execution';

// Exported so callers outside this module (e.g., the Execute page's
// SIWE bind-to-current flow) can invalidate the cache with the canonical
// key shape rather than hardcoding a literal that drifts silently.
export const KEY_WALLET_ME = ['wallet', 'me'] as const;
const KEY_ORDERS = ['execution', 'orders'] as const;
const KEY_POSITIONS = ['execution', 'positions'] as const;
const KEY_SYMBOLS = ['execution', 'symbols'] as const;
// Cache key for the SoDEX bootstrap (auto-fetched account_id +
// per-venue named keys). Same shape as KEY_WALLET_ME — the
// `address` slot lets us refetch independently per wallet if the
// page ever supports wallet switching mid-session.
const KEY_SODEX_BOOTSTRAP = ['wallet', 'sodex-bootstrap'] as const;

// ---------------------------------------------------------------------------
// Reads
// ---------------------------------------------------------------------------

export function useWalletMe() {
  return useQuery({
    queryKey: KEY_WALLET_ME,
    queryFn: fetchWalletMe,
    refetchInterval: 30_000,
  });
}

/**
 * Discover the wallet's SoDEX account_id + per-venue named API keys
 * so the FE can skip the manual ApiKeyForm. Enabled only when the
 * caller has a bound wallet — there's nothing to discover otherwise
 * AND the backend returns 403.
 *
 * `staleTime: 30s` matches `useWalletMe` so a key registered on the
 * SoDEX dashboard mid-session is picked up on the next refetch.
 * Retry off — 503 means SoDEX is unreachable, and the consumer
 * falls back to today's manual form (graceful degrade).
 */
export function useSodexBootstrap(walletAddress: string | null) {
  return useQuery({
    queryKey: KEY_SODEX_BOOTSTRAP,
    queryFn: fetchSodexBootstrap,
    enabled: !!walletAddress,
    staleTime: 30_000,
    retry: false,
  });
}

export function useOrders(params?: { venue?: Venue; status?: string }) {
  // List filters are folded into the queryKey so distinct filter combos
  // get their own cache slot. Mutations invalidate the BASE key so all
  // filter views refetch on the next render.
  return useQuery({
    queryKey: [...KEY_ORDERS, params?.venue ?? null, params?.status ?? null],
    queryFn: () => fetchOrders(params),
    refetchInterval: 5_000,
  });
}

export function usePositions() {
  return useQuery({
    queryKey: KEY_POSITIONS,
    queryFn: fetchPositions,
    refetchInterval: 15_000,
  });
}

export function useSymbols(venue?: Venue) {
  return useQuery({
    queryKey: [...KEY_SYMBOLS, venue ?? null],
    queryFn: () => fetchSymbols(venue),
    // Symbols change rarely (new SoDEX listings). Skip the polling
    // cadence; rely on the 30s staleTime default + manual invalidate.
  });
}

// ---------------------------------------------------------------------------
// Mutations
// ---------------------------------------------------------------------------

export function useSetApiKey() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (req: SetApiKeyRequest) => postWalletApiKey(req),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: KEY_WALLET_ME });
    },
  });
}

/**
 * #185 — paper-trade user requests live trading. Operator gets a Telegram
 * message; route does NOT flip `paper_trade` (operator action via the
 * admin route is the only path that changes execution behaviour).
 *
 * No cache invalidation on success — the user's state is unchanged
 * (still paper_trade=true). The FE reads the response message and
 * shows a toast/inline confirmation.
 */
export function useRequestLive() {
  return useMutation<RequestLiveResponse, unknown, RequestLiveRequest>({
    mutationFn: (req) => postRequestLive(req),
  });
}

export function usePrepareNew() {
  // No optimistic update — the order_id only exists post-INSERT. UI
  // shows a spinner during the call, then renders the typed-data for
  // signing.
  return useMutation<PrepareNewResponse, unknown, PrepareNewRequest>({
    mutationFn: postPrepareNew,
  });
}

export function useSubmitNew() {
  const qc = useQueryClient();
  return useMutation<SubmitResponse, unknown, { orderId: number; signature: string }>({
    mutationFn: ({ orderId, signature }) => postSubmitNew(orderId, signature),
    onSettled: () => {
      // Order status + position state can both move on submit. Invalidate
      // both. `onSettled` (not `onSuccess`) so a partial failure that
      // still mutated DB state still refreshes the UI.
      qc.invalidateQueries({ queryKey: KEY_ORDERS });
      qc.invalidateQueries({ queryKey: KEY_POSITIONS });
    },
  });
}

export function usePrepareCancel() {
  return useMutation<PrepareCancelResponse, unknown, number>({
    mutationFn: postPrepareCancel,
  });
}

export function useSubmitCancel() {
  const qc = useQueryClient();
  return useMutation<SubmitResponse, unknown, { orderId: number; signature: string }>({
    mutationFn: ({ orderId, signature }) => postSubmitCancel(orderId, signature),
    onSettled: () => {
      qc.invalidateQueries({ queryKey: KEY_ORDERS });
      qc.invalidateQueries({ queryKey: KEY_POSITIONS });
    },
  });
}

/** PR P1.4 — build typed-data for closing an open position. Same
 *  PrepareNew response shape; the caller still has to sign + submit
 *  via `useSubmitNew` to land the order on the gateway. */
export function useClosePosition() {
  return useMutation<PrepareNewResponse, unknown, number>({
    mutationFn: postClosePosition,
  });
}
