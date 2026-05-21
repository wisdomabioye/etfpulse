/**
 * Telegram WebApp detection + initData accessor.
 *
 * `window.Telegram.WebApp` is populated by Telegram's WebApp SDK
 * (`telegram-web-app.js`, loaded in `index.html` BEFORE the React
 * bundle). When the page is opened OUTSIDE a Telegram WebApp context
 * (a regular browser tab, server-side render, etc.) the SDK script
 * is still fetched but `window.Telegram` stays undefined — `detectWebApp()`
 * returns null on that path.
 *
 * The SDK shape we depend on:
 *   - `WebApp.initData: string` — the raw query-string Telegram
 *     emitted at WebApp launch. The bytes are HMAC-signed by Telegram
 *     using our bot_token; the backend's `auth_telegram.verify_webapp_init_data`
 *     re-checks the signature.
 *   - `WebApp.ready(): void` — tell Telegram the SPA finished
 *     mounting so the loading splash dismisses.
 *   - `WebApp.expand(): void` — request the bottom-sheet expand to
 *     full height (better UX on mobile).
 *
 * No types from `@telegram-apps/sdk` etc. — those packages are
 * unnecessary for our minimal usage. A narrow interface declared
 * here keeps the surface auditable + the bundle slim.
 */

interface TelegramWebApp {
  /** Raw initData query-string. Empty when launched outside an
   *  initialized session (rare; cold-start race). */
  readonly initData: string;
  /** `themeParams.bg_color` etc. — optional; we don't read it today. */
  readonly themeParams?: Record<string, string>;
  /** Notify Telegram the page is ready (dismisses splash). */
  ready: () => void;
  /** Expand the WebApp to full viewport height. */
  expand: () => void;
}

interface TelegramSdk {
  WebApp?: TelegramWebApp;
}

declare global {
  interface Window {
    Telegram?: TelegramSdk;
  }
}

/**
 * Return the WebApp object when running inside Telegram, else null.
 *
 * Safe to call during render — does not mutate state. The check is
 * stable across re-renders within the same page load (Telegram's
 * SDK populates the global once at script load).
 */
export function detectWebApp(): TelegramWebApp | null {
  if (typeof window === 'undefined') return null;
  return window.Telegram?.WebApp ?? null;
}

/**
 * Return the raw initData string when we're in a Telegram WebApp
 * with an actual session, else null.
 *
 * A non-null return means we have something the backend's
 * `/api/auth/telegram/verify` can validate. Empty initData (rare
 * cold-start race) returns null so callers don't POST empty bodies
 * the backend would reject as `missing init_data`.
 */
export function getInitDataRaw(): string | null {
  const app = detectWebApp();
  if (!app || !app.initData) return null;
  return app.initData;
}

/**
 * Notify Telegram that the SPA finished mounting + expand the
 * WebApp viewport. Safe to call when not in a WebApp (no-ops).
 *
 * Callers should fire this from a top-level `useEffect` so it runs
 * once after first paint, not on every render.
 */
export function announceWebAppReady(): void {
  const app = detectWebApp();
  if (!app) return;
  try {
    app.ready();
    app.expand();
  } catch {
    // Defensive — SDK errors on call shouldn't crash the SPA.
  }
}
