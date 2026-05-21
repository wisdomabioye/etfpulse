/**
 * Telegram WebApp auto-login orchestrator.
 *
 * Companion to D.4.2's `siwe.ts`. Where SIWE proves wallet ownership
 * via a signed message, this path proves Telegram identity via
 * Telegram's HMAC over `initData`. The backend route
 * (`POST /api/auth/telegram/verify`) does the actual HMAC check + JWT
 * mint; the FE just forwards the raw initData and stashes the JWT.
 *
 * UX contract: silent auto-login. When the SPA boots inside a
 * Telegram WebApp AND no JWT is already in storage, AuthProvider
 * fires this orchestrator once. Success → JWT stored → page renders
 * authed. Failure → error is suppressed (logged once) and the
 * normal SIWE login flow remains available.
 *
 * Why silent: a Telegram WebApp launch already implied the user
 * intends to use ETFPulse. Showing a "Verifying…" gate before the
 * page renders adds latency and a UI surface to a flow Telegram
 * already authenticated.
 */

import { apiPost } from '../api/client';

interface TelegramVerifyResponse {
  jwt: string;
  user_id: number;
  telegram_user_id: number;
  has_wallet: boolean;
}

/**
 * Forward `initDataRaw` to the backend for HMAC verification +
 * JWT mint. Returns the response or throws.
 *
 * The raw initData MUST be the query-string form Telegram emitted —
 * not pre-decoded. The backend's HMAC is computed against the
 * post-URL-decoded `k=v\n...` form, so the wire shape we forward
 * matches what Telegram signed.
 */
export async function performTelegramVerify(
  initDataRaw: string,
): Promise<TelegramVerifyResponse> {
  return apiPost<TelegramVerifyResponse>('/api/auth/telegram/verify', {
    init_data: initDataRaw,
  });
}
