/**
 * Flow tests for `performSiweLogin` (#78.3).
 *
 * Pins the "FE trusts backend-issued fields" contract — every load-
 * bearing field of the SIWE message (domain, uri, chainId, nonce,
 * statement, issuedAt, expirationTime) is embedded VERBATIM from the
 * `/api/wallet/nonce` response. If a future refactor adds FE-side
 * reconstruction (e.g., reading `chainId` from `VITE_SODEX_CHAIN_ID`),
 * the backend's verify path would mismatch and every SIWE 400s.
 *
 * Mocking strategy:
 *   - `fetch` is `vi.spyOn`'d on `globalThis` so the api/client wrapper
 *     hits our stub.
 *   - `signMessageAsync` is a plain stub that captures the message it
 *     was asked to sign — that's the source of truth for "what did we
 *     ask the wallet to sign?"
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { performSiweLogin } from './siwe';

// 64 chars of `c` so it's distinguishable from `r`/`s` in the wire format.
const FAKE_SIGNATURE = `0x${'c'.repeat(130)}`;

const FAKE_ADDRESS = '0xabcdef0123456789abcdef0123456789abcdef01' as const;

// Construct a realistic NonceResponse payload mirroring backend's
// `api/schemas/wallet.py:NonceResponse`. Operator-tunable fields are
// chosen to be distinct from FE defaults so a leak would jump out.
const NONCE_BODY = {
  nonce: 'abcdef0123456789',
  statement: 'Sign in to ETFPulse to manage your trading account.',
  domain: 'etfpulse.example.com',
  uri: 'https://etfpulse.example.com',
  chain_id: 138565,
  version: '1' as const,
  issued_at: '2026-05-23T12:00:00.000Z',
  expires_at: '2026-05-23T12:10:00.000Z',
};

const VERIFY_BODY = {
  jwt: 'header.payload.signature',
  user_id: 42,
  wallet_address: FAKE_ADDRESS,
};

/**
 * Stub fetch — returns staged JSON for the two endpoints we expect to
 * be called, throws on anything else (catches a route typo at the call
 * site).
 */
function stubFetch(
  capturedRequests: Array<{ url: string; init?: RequestInit }>,
  responses: { nonce?: unknown; verify?: unknown },
) {
  return vi.spyOn(globalThis, 'fetch').mockImplementation((url, init) => {
    capturedRequests.push({ url: String(url), init: init as RequestInit });
    if (String(url).endsWith('/api/wallet/nonce')) {
      return Promise.resolve(
        new Response(JSON.stringify(responses.nonce ?? NONCE_BODY), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      );
    }
    if (String(url).endsWith('/api/wallet/verify')) {
      return Promise.resolve(
        new Response(JSON.stringify(responses.verify ?? VERIFY_BODY), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      );
    }
    throw new Error(`Unexpected fetch URL: ${url}`);
  });
}

// Explicit signature alias — match siwe.ts's local `SignMessageFn`
// type. `vi.fn` needs the generic to narrow the mock's call shape down
// to what `performSiweLogin` expects; without it, tsc rejects the
// pass-through at the call site.
type SignMessageFn = (args: { message: string }) => Promise<string>;

describe('performSiweLogin — happy path', () => {
  let capturedRequests: Array<{ url: string; init?: RequestInit }>;
  let signedMessage = '';
  let signMessageStub: ReturnType<typeof vi.fn<SignMessageFn>>;

  beforeEach(() => {
    capturedRequests = [];
    signedMessage = '';
    signMessageStub = vi.fn<SignMessageFn>(async ({ message }) => {
      signedMessage = message;
      return FAKE_SIGNATURE;
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('returns the verify response on success', async () => {
    stubFetch(capturedRequests, {});
    const result = await performSiweLogin({
      address: FAKE_ADDRESS,
      signMessageAsync: signMessageStub,
    });
    expect(result).toEqual(VERIFY_BODY);
  });

  it('requests nonce with the address in the body', async () => {
    stubFetch(capturedRequests, {});
    await performSiweLogin({ address: FAKE_ADDRESS, signMessageAsync: signMessageStub });

    const nonceReq = capturedRequests.find((r) => r.url.endsWith('/api/wallet/nonce'));
    expect(nonceReq).toBeDefined();
    expect(nonceReq?.init?.method).toBe('POST');
    expect(JSON.parse(nonceReq?.init?.body as string)).toEqual({ address: FAKE_ADDRESS });
  });

  it('embeds backend-supplied chainId in the SIWE message (NOT a FE constant)', async () => {
    // Use a deliberately weird chainId so a FE-hardcoded value would
    // not match by coincidence.
    stubFetch(capturedRequests, { nonce: { ...NONCE_BODY, chain_id: 999888 } });
    await performSiweLogin({ address: FAKE_ADDRESS, signMessageAsync: signMessageStub });
    expect(signedMessage).toContain('Chain ID: 999888');
  });

  it('embeds backend-supplied domain in the SIWE message', async () => {
    stubFetch(capturedRequests, {
      nonce: { ...NONCE_BODY, domain: 'staging.etfpulse.dev' },
    });
    await performSiweLogin({ address: FAKE_ADDRESS, signMessageAsync: signMessageStub });
    expect(signedMessage).toContain('staging.etfpulse.dev wants you to sign in');
  });

  it('embeds backend-supplied uri in the SIWE message', async () => {
    stubFetch(capturedRequests, {
      nonce: { ...NONCE_BODY, uri: 'https://staging.etfpulse.dev' },
    });
    await performSiweLogin({ address: FAKE_ADDRESS, signMessageAsync: signMessageStub });
    expect(signedMessage).toContain('URI: https://staging.etfpulse.dev');
  });

  it('embeds backend-supplied nonce in the SIWE message', async () => {
    // viem enforces nonce alphanumeric-only (EIP-4361 §2.3); backend's
    // `secrets.token_hex(N)` output is alphanumeric, so this matches
    // production shape.
    stubFetch(capturedRequests, {
      nonce: { ...NONCE_BODY, nonce: 'uniqnonce1234abcd' },
    });
    await performSiweLogin({ address: FAKE_ADDRESS, signMessageAsync: signMessageStub });
    expect(signedMessage).toContain('Nonce: uniqnonce1234abcd');
  });

  it('embeds backend-supplied statement in the SIWE message', async () => {
    stubFetch(capturedRequests, {
      nonce: { ...NONCE_BODY, statement: 'Sign in to acknowledge the risks.' },
    });
    await performSiweLogin({ address: FAKE_ADDRESS, signMessageAsync: signMessageStub });
    expect(signedMessage).toContain('Sign in to acknowledge the risks.');
  });

  it('forwards the exact signature + message to /verify', async () => {
    stubFetch(capturedRequests, {});
    await performSiweLogin({ address: FAKE_ADDRESS, signMessageAsync: signMessageStub });

    const verifyReq = capturedRequests.find((r) => r.url.endsWith('/api/wallet/verify'));
    expect(verifyReq).toBeDefined();
    expect(verifyReq?.init?.method).toBe('POST');
    const body = JSON.parse(verifyReq?.init?.body as string);
    expect(body).toEqual({ message: signedMessage, signature: FAKE_SIGNATURE });
  });

  it('calls nonce BEFORE verify (sequence matters — verify consumes nonce)', async () => {
    stubFetch(capturedRequests, {});
    await performSiweLogin({ address: FAKE_ADDRESS, signMessageAsync: signMessageStub });

    const nonceIdx = capturedRequests.findIndex((r) => r.url.endsWith('/api/wallet/nonce'));
    const verifyIdx = capturedRequests.findIndex((r) => r.url.endsWith('/api/wallet/verify'));
    expect(nonceIdx).toBeGreaterThanOrEqual(0);
    expect(verifyIdx).toBeGreaterThanOrEqual(0);
    expect(nonceIdx).toBeLessThan(verifyIdx);
  });
});

describe('performSiweLogin — error propagation', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('throws when nonce request fails (e.g., 503 — domain misconfig)', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation((url) => {
      if (String(url).endsWith('/api/wallet/nonce')) {
        return Promise.resolve(
          new Response(JSON.stringify({ detail: 'siwe_domain_not_configured' }), {
            status: 503,
            headers: { 'Content-Type': 'application/json' },
          }),
        );
      }
      throw new Error(`Should not have reached: ${url}`);
    });

    const signStub = vi.fn<SignMessageFn>(async () => FAKE_SIGNATURE);
    await expect(
      performSiweLogin({ address: FAKE_ADDRESS, signMessageAsync: signStub }),
    ).rejects.toThrow();
    // Signature MUST NOT have been requested — flow stops at nonce.
    expect(signStub).not.toHaveBeenCalled();
  });

  it('throws when the user rejects the wallet signature prompt', async () => {
    const capturedRequests: Array<{ url: string; init?: RequestInit }> = [];
    stubFetch(capturedRequests, {});
    const signStub = vi.fn<SignMessageFn>(async () => {
      throw new Error('User rejected the request');
    });
    await expect(
      performSiweLogin({ address: FAKE_ADDRESS, signMessageAsync: signStub }),
    ).rejects.toThrow('User rejected the request');
    // /verify MUST NOT have been called — no signature to forward.
    expect(capturedRequests.find((r) => r.url.endsWith('/api/wallet/verify'))).toBeUndefined();
  });

  it('throws when verify rejects the signature (e.g., 400 invalid_hash)', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation((url) => {
      if (String(url).endsWith('/api/wallet/nonce')) {
        return Promise.resolve(
          new Response(JSON.stringify(NONCE_BODY), { status: 200 }),
        );
      }
      if (String(url).endsWith('/api/wallet/verify')) {
        return Promise.resolve(
          new Response(JSON.stringify({ detail: 'invalid_hash' }), { status: 400 }),
        );
      }
      throw new Error(`Unexpected: ${url}`);
    });

    const signStub = vi.fn<SignMessageFn>(async () => FAKE_SIGNATURE);
    await expect(
      performSiweLogin({ address: FAKE_ADDRESS, signMessageAsync: signStub }),
    ).rejects.toThrow();
  });
});
