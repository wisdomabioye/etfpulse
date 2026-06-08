/**
 * Typed wrappers for the PR D.4 execution + wallet routes.
 *
 * Types mirror `etfpulse/api/schemas/execution.py` and
 * `etfpulse/api/schemas/wallet.py` — keep in sync when the backend
 * shapes change. Anti-drift: a backend response_model change MUST
 * land here in the same PR.
 *
 * Wire-format reminders:
 *   - All decimals come through as strings (Pydantic serialises Decimal
 *     as string to avoid float precision loss). The FE treats them as
 *     strings — number parsing happens at display time, not on the
 *     query path, so the user sees the exact backend value.
 *   - `venue` is the StrEnum value ("sodex_spot" | "sodex_perps").
 *   - `signature` posted to /submit MUST be the 0x01-prefixed +
 *     v-normalized form from `toSodexTypedSignature`.
 */

import { apiGet, apiPost } from './client';

// ---------------------------------------------------------------------------
// Wallet (D.4.2)
// ---------------------------------------------------------------------------

export interface WalletMeResponse {
  user_id: number;
  wallet_address: string | null;
  sodex_account_id: number | null;
  paper_trade: boolean;
  sodex_spot_api_key_name: string | null;
  sodex_perps_api_key_name: string | null;
}

export interface SetApiKeyRequest {
  venue: 'sodex_spot' | 'sodex_perps';
  api_key_name: string;
  sodex_account_id: number;
}

export interface SetApiKeyResponse {
  ok: true;
  venue: string;
  api_key_name: string;
  sodex_account_id: number;
}

/**
 * One named API key as returned by `/api/wallet/sodex-bootstrap`.
 * Mirrors `etfpulse/api/schemas/wallet.py:APIKeyOut` field-for-field.
 * `expires_at: 0` means "never expires" per SoDEX api.md.
 */
export interface APIKeyOut {
  name: string;
  public_key: string;
  expires_at: number;
}

/**
 * Response shape of `GET /api/wallet/sodex-bootstrap`. Mirrors
 * `SodexBootstrapResponse` on the backend. Any of the three fields
 * can independently land in its "missing" state:
 *   - account_id null: wallet has no SoDEX account yet
 *   - spot_keys / perps_keys empty: no named keys on that venue
 */
export interface SodexBootstrapResponse {
  account_id: number | null;
  spot_keys: APIKeyOut[];
  perps_keys: APIKeyOut[];
}

// ---------------------------------------------------------------------------
// Execution (D.4.3)
// ---------------------------------------------------------------------------

export type Venue = 'sodex_spot' | 'sodex_perps';
export type Side = 'buy' | 'sell';
export type OrderType = 'limit' | 'market';
export type TimeInForce = 'gtc' | 'ioc' | 'fok' | 'gtx';
export type PositionSide = 'both' | 'long' | 'short';

export interface PrepareNewRequest {
  venue: Venue;
  asset: string;
  side: Side;
  order_type: OrderType;
  time_in_force: TimeInForce;
  requested_size: string;
  requested_price?: string | null;
  position_side?: PositionSide | null;
  leverage?: string | null;
  signal_id?: number | null;
}

// EIP-712 typed-data envelope viem's `signTypedData` consumes. The
// backend hands this back verbatim from `pipeline.execution.builders`.
export interface TypedData {
  types: Record<string, { name: string; type: string }[]>;
  primaryType: string;
  domain: {
    name: string;
    version: string;
    chainId: number;
    verifyingContract: string;
  };
  message: Record<string, unknown>;
}

export interface PrepareNewResponse {
  order_id: number;
  client_order_id: string;
  nonce: number;
  typed_data: TypedData;
}

export interface PrepareCancelResponse {
  order_id: number;
  typed_data: TypedData | null;
  client_order_id: string | null;
  nonce: number | null;
  local_only: boolean;
  replayed: boolean;
}

export interface SubmitResponse {
  order_id: number;
  status: string;
  exchange_order_id: string | null;
  error_message: string | null;
  replayed: boolean;
}

export interface OrderOut {
  id: number;
  user_id: number | null;
  signal_id: number | null;
  venue: Venue;
  asset: string;
  side: string;
  order_type: string;
  time_in_force: string;
  requested_size: string;
  requested_price: string | null;
  filled_size: string | null;
  filled_price: string | null;
  filled_value: string | null;
  fees: string | null;
  status: string;
  exchange_order_id: string | null;
  client_order_id: string;
  error_message: string | null;
  paper_trade: boolean;
  account_id: number | null;
  symbol_id: number | null;
  nonce: number | null;
  nonce_expires_at: string | null;
  expires_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface PaginatedOrders {
  items: OrderOut[];
  total: number;
  limit: number;
  offset: number;
}

export interface PositionOut {
  id: number;
  user_id: number | null;
  venue: Venue;
  asset: string;
  side: string;
  size: string;
  entry_price: string;
  stop_loss: string | null;
  take_profit: string | null;
  status: string;
  paper_trade: boolean;
  leverage: string | null;
  opened_at: string;
  closed_at: string | null;
  close_price: string | null;
  realized_pnl: string | null;
}

export interface PositionsResponse {
  items: PositionOut[];
}

export interface SymbolOut {
  venue: Venue;
  symbol_id: number;
  name: string;
  asset: string;
}

export interface SymbolsResponse {
  items: SymbolOut[];
}

// ---------------------------------------------------------------------------
// Wallet API wrappers
// ---------------------------------------------------------------------------

export function fetchWalletMe(): Promise<WalletMeResponse> {
  return apiGet<WalletMeResponse>('/api/wallet/me');
}

export function postWalletApiKey(req: SetApiKeyRequest): Promise<SetApiKeyResponse> {
  return apiPost<SetApiKeyResponse>('/api/wallet/api-key', req);
}

export function fetchSodexBootstrap(): Promise<SodexBootstrapResponse> {
  return apiGet<SodexBootstrapResponse>('/api/wallet/sodex-bootstrap');
}

export interface RequestLiveRequest {
  note?: string;
}

export interface RequestLiveResponse {
  ok: true;
  message: string;
}

export function postRequestLive(req: RequestLiveRequest): Promise<RequestLiveResponse> {
  return apiPost<RequestLiveResponse>('/api/wallet/request-live', req);
}

// ---------------------------------------------------------------------------
// Execution API wrappers
// ---------------------------------------------------------------------------

export function postPrepareNew(req: PrepareNewRequest): Promise<PrepareNewResponse> {
  return apiPost<PrepareNewResponse>('/api/execution/prepare', req);
}

export function postSubmitNew(orderId: number, signature: string): Promise<SubmitResponse> {
  return apiPost<SubmitResponse>(`/api/execution/submit/${orderId}`, { signature });
}

export function postPrepareCancel(orderId: number): Promise<PrepareCancelResponse> {
  return apiPost<PrepareCancelResponse>(`/api/execution/prepare-cancel/${orderId}`);
}

export function postSubmitCancel(orderId: number, signature: string): Promise<SubmitResponse> {
  return apiPost<SubmitResponse>(`/api/execution/submit-cancel/${orderId}`, { signature });
}

export function fetchOrders(params?: {
  venue?: Venue;
  status?: string;
  limit?: number;
  offset?: number;
}): Promise<PaginatedOrders> {
  return apiGet<PaginatedOrders>('/api/execution/orders', params);
}

export function fetchPositions(): Promise<PositionsResponse> {
  return apiGet<PositionsResponse>('/api/execution/positions');
}

export function fetchSymbols(venue?: Venue): Promise<SymbolsResponse> {
  return apiGet<SymbolsResponse>('/api/execution/symbols', venue ? { venue } : undefined);
}
