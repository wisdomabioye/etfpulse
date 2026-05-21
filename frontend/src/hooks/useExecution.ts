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
  type SetApiKeyRequest,
  type SubmitResponse,
  type Venue,
  fetchOrders,
  fetchPositions,
  fetchSymbols,
  fetchWalletMe,
  postPrepareCancel,
  postPrepareNew,
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
