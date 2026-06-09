/**
 * Typed fetch wrapper.
 *
 * Dev: VITE_API_BASE_URL is empty → fetch uses relative `/api/...` →
 *      Vite proxy forwards to backend on :8000. No CORS friction.
 * Prod: VITE_API_BASE_URL set on Vercel → fetch uses absolute URL →
 *       backend's CORS_ORIGINS allows the Vercel origin.
 *
 * All non-2xx responses throw `ApiError`. Components can narrow with
 * `if (error instanceof ApiError) ...` to read .status / .detail.
 */

import { getJwt } from '../auth/storage';

const API_BASE = import.meta.env.VITE_API_BASE_URL || '';

// 401-handler callback. The AuthProvider registers a cleanup function
// (`setJwt(null)` + redirect to /login) at mount; the API client calls
// it whenever a request comes back 401. Registering the callback via
// setter rather than React Context keeps this module React-free —
// `apiGet`/`apiPost` are called from query functions outside the
// component tree.
let onUnauthorized: (() => void) | null = null;

// Coalesce-window flag: a page-mount that fan-outs N parallel queries
// against a stale JWT would produce N 401s and N navigate() calls.
// Functionally idempotent (subsequent setJwt/navigate are no-ops) but
// noisy + wastes render cycles. The flag latches on first 401 and
// auto-resets on next macrotask so a *subsequent* 401 (e.g., user signs
// back in, then second token expires) still fires the handler.
let unauthorizedLatched = false;

/**
 * Wire up a callback invoked once per 401 response. The auth layer
 * sets this at mount with a logout-and-redirect handler. Re-setting
 * replaces the prior callback (no fan-out — there's only one auth
 * layer).
 */
export function setOnUnauthorized(cb: (() => void) | null): void {
  onUnauthorized = cb;
}

export class ApiError extends Error {
  // Explicit fields rather than constructor parameter properties — TS 6's
  // `erasableSyntaxOnly` flag (set in tsconfig.app.json by Vite's React preset)
  // rejects the parameter-property shorthand because it's not pure type erasure.
  readonly status: number;
  readonly detail: string;

  constructor(status: number, detail: string) {
    super(`API ${status}: ${detail}`);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
  }
}

/** Build the full URL with query string (skipping null/undefined params).
 *
 * Accepts `object` (not `Record<string, unknown>`) because TS treats the
 * SignalFilters interface as not having an index signature, so it doesn't
 * widen to Record. `object` accepts any non-null reference type and
 * `Object.entries` handles it correctly.
 */
function buildUrl(path: string, params?: object): string {
  if (!params) return `${API_BASE}${path}`;
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null) continue;
    search.set(key, String(value));
  }
  const qs = search.toString();
  return qs ? `${API_BASE}${path}?${qs}` : `${API_BASE}${path}`;
}

/** Build the default headers for any request, auto-stamping
 *  `Authorization: Bearer <jwt>` when a token is present. `extraHeaders`
 *  wins on key conflict so callers can override (e.g., admin route
 *  using `X-Admin-Key`, or the wallet routes deliberately sending no
 *  token during the SIWE bind).
 */
function buildHeaders(extraHeaders?: Record<string, string>): Record<string, string> {
  const jwt = getJwt();
  return {
    Accept: 'application/json',
    ...(jwt ? { Authorization: `Bearer ${jwt}` } : {}),
    ...(extraHeaders ?? {}),
  };
}

export async function apiGet<T>(
  path: string,
  params?: object,
  extraHeaders?: Record<string, string>,
): Promise<T> {
  const res = await fetch(buildUrl(path, params), {
    headers: buildHeaders(extraHeaders),
  });
  return handleResponse<T>(res);
}

/** POST helper for admin mutations. JSON body is optional — endpoints like
 *  `/api/admin/signals/trigger` take no body but still require POST. When
 *  `body` is undefined the request omits Content-Type/body entirely so a
 *  zero-byte POST cleanly reaches FastAPI; passing `{}` would send a
 *  Content-Type that confuses some servers about whether a body is
 *  expected. Headers shape matches `apiGet` so callers can stamp
 *  `X-Admin-Key` uniformly. */
export async function apiPost<T>(
  path: string,
  body?: unknown,
  extraHeaders?: Record<string, string>,
): Promise<T> {
  const init: RequestInit = {
    method: 'POST',
    headers: buildHeaders(extraHeaders),
  };
  if (body !== undefined) {
    init.headers = { ...init.headers, 'Content-Type': 'application/json' };
    init.body = JSON.stringify(body);
  }
  const res = await fetch(buildUrl(path), init);
  return handleResponse<T>(res);
}

async function handleResponse<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let detail = res.statusText;
    try {
      // FastAPI errors serialize as { "detail": "..." }. If the body isn't
      // JSON or doesn't follow that shape, fall back to status text.
      const body = await res.json();
      if (body && typeof body.detail === 'string') {
        detail = body.detail;
      } else if (body && body.detail && typeof body.detail === 'object') {
        // Structured errors (e.g. the execution risk gate's 403 DENY:
        // `{ reason, detail, breaker_trigger }`). Surface the human
        // message, then the reason code, then a JSON fallback — never
        // let a structured body collapse to a generic "Forbidden".
        const d = body.detail as Record<string, unknown>;
        detail =
          typeof d.detail === 'string'
            ? d.detail
            : typeof d.reason === 'string'
              ? d.reason
              : JSON.stringify(d);
      }
    } catch {
      // ignore parse errors
    }
    // 401 → token is invalid/expired/missing. Notify the auth layer
    // so it can clear storage + redirect to /login. We still throw
    // the ApiError so the calling component knows the request failed.
    //
    // Coalesce parallel 401s within the same event-loop turn — N queries
    // failing simultaneously on a stale JWT must not trigger N navigates.
    // `setTimeout(0)` schedules the reset on the next macrotask, which
    // runs AFTER the current microtask queue (the parallel fetch
    // promises). A subsequent 401 in a *later* turn still fires.
    if (res.status === 401) {
      if (!unauthorizedLatched) {
        unauthorizedLatched = true;
        setTimeout(() => {
          unauthorizedLatched = false;
        }, 0);
        onUnauthorized?.();
      }
    }
    throw new ApiError(res.status, detail);
  }
  return res.json() as Promise<T>;
}
