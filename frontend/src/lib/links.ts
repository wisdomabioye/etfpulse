/**
 * Static external links surfaced in the UI.
 *
 * Keeping these in one module avoids drift when the same URL appears in
 * multiple places (Footer + signal-detail CTA + future onboarding banners).
 * Update here, every consumer reflects the change.
 *
 * NOT environment-driven — these are public, brand-stable URLs. If a future
 * stage needs per-environment overrides (e.g. a staging bot), promote to
 * `import.meta.env.VITE_*` reads with these as fallbacks.
 */

/** Direct DM with the ETFPulse bot. Used by future "Connect to alerts" CTAs. */
export const TELEGRAM_BOT_URL = 'https://t.me/eftpulse_bot';

/** Public ETFPulse community/discussion group. */
export const TELEGRAM_GROUP_URL = 'https://t.me/eft_pulse';

/** Keyed accessor — same values, exposed as an object so consumers that want
 *  to pass a "telegram link" prop into a generic component can do so without
 *  importing each URL individually. */
export const LINKS = {
  telegramBot: TELEGRAM_BOT_URL,
  telegramGroup: TELEGRAM_GROUP_URL,
} as const;
