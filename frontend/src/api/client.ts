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

const API_BASE = import.meta.env.VITE_API_BASE_URL || '';

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

export async function apiGet<T>(
  path: string,
  params?: object,
  extraHeaders?: Record<string, string>,
): Promise<T> {
  const res = await fetch(buildUrl(path, params), {
    headers: { Accept: 'application/json', ...(extraHeaders ?? {}) },
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
    headers: { Accept: 'application/json', ...(extraHeaders ?? {}) },
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
      }
    } catch {
      // ignore parse errors
    }
    throw new ApiError(res.status, detail);
  }
  return res.json() as Promise<T>;
}
