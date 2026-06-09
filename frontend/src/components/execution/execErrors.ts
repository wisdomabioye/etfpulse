import { ApiError } from '../../api/client';

/**
 * Normalise any thrown error into one operator-grep-friendly line. FastAPI
 * structured errors (e.g. a 403 risk DENY) carry a JSON detail object — the
 * client (`api/client.ts`) already flattens it to a string; we just prefix the
 * status. Wallet rejections collapse to a friendly message.
 */
export function formatError(e: unknown): string {
  if (e instanceof ApiError) {
    return `[${e.status}] ${e.detail}`;
  }
  if (e instanceof Error) {
    if (/rejected|denied/i.test(e.message)) return 'Wallet signature rejected.';
    return e.message;
  }
  return String(e);
}
