/**
 * SIWE (Sign-In-With-Ethereum) flow orchestrator.
 *
 * Three-step ceremony, called from the Login page after the wallet is
 * connected:
 *
 *   1. POST /api/wallet/nonce          — backend issues single-use nonce
 *      + everything we need to build the SIWE message (domain, uri,
 *      chainId, statement, issued_at, expires_at).
 *   2. Build SIWE message via viem `createSiweMessage` + sign via
 *      wagmi `signMessageAsync`.
 *   3. POST /api/wallet/verify         — backend verifies sig, mints JWT.
 *
 * The orchestrator is a plain async function — UI concerns (button
 * state, error toast) live in the calling component. We return the
 * verify response or throw; callers `.catch()` for UX.
 *
 * Domain/chainId integrity: the backend's `NonceResponse` carries the
 * `domain`/`uri`/`chain_id`/`statement` it expects to see back in the
 * verify step. We embed them verbatim into the SIWE message — there's
 * no place for FE drift because the message uses backend-supplied
 * values. The only FE-controlled field is the address (from wagmi),
 * which the backend cross-checks against the recovered signer.
 */

import { createSiweMessage } from 'viem/siwe';

import { apiPost } from '../api/client';

interface NonceResponse {
  nonce: string;
  statement: string;
  domain: string;
  uri: string;
  chain_id: number;
  version: '1';
  issued_at: string;
  expires_at: string;
}

interface VerifyResponse {
  jwt: string;
  user_id: number;
  wallet_address: string;
}

/**
 * Callback shape matching wagmi's `useSignMessage().signMessageAsync`.
 * Declaring it locally avoids a hard dependency on wagmi types in
 * this orchestrator — callers can also pass a test stub.
 */
type SignMessageFn = (args: { message: string }) => Promise<string>;

/**
 * Run the full SIWE login flow. Returns the verify response (JWT +
 * user_id + wallet_address) on success.
 *
 * Throws `ApiError` from the API client on backend failure (nonce
 * 503, verify 400 for various reasons — see auth_siwe.py detail
 * strings). Throws a wallet error if the user rejects the signature
 * prompt (wagmi surfaces this as a thrown error from signMessageAsync).
 */
export async function performSiweLogin(args: {
  address: `0x${string}`;
  signMessageAsync: SignMessageFn;
}): Promise<VerifyResponse> {
  const { address, signMessageAsync } = args;

  // Step 1 — request nonce. Backend returns 503 if SIWE domain isn't
  // configured (FRONTEND_URL empty); the route returns 422 if address
  // shape is wrong (caught before this call by the FE address check).
  const nonceResp = await apiPost<NonceResponse>('/api/wallet/nonce', { address });

  // Step 2 — build SIWE message using backend-supplied fields, sign.
  // viem will EIP-55-checksum the address — the backend lowercases on
  // verify, so casing doesn't matter cross-side.
  const message = createSiweMessage({
    address,
    domain: nonceResp.domain,
    uri: nonceResp.uri,
    chainId: nonceResp.chain_id,
    nonce: nonceResp.nonce,
    statement: nonceResp.statement,
    version: nonceResp.version,
    issuedAt: new Date(nonceResp.issued_at),
    expirationTime: new Date(nonceResp.expires_at),
  });
  const signature = await signMessageAsync({ message });

  // Step 3 — verify. On success the backend mints the JWT; we return
  // it for the caller to stash in storage. The verify path consumes
  // the nonce, so a retry of this exact call would 400.
  const verifyResp = await apiPost<VerifyResponse>('/api/wallet/verify', {
    message,
    signature,
  });
  return verifyResp;
}
