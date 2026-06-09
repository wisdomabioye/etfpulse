/**
 * `color-mix()` helper for DATA-DRIVEN colors (R0 / decision D1-B).
 *
 * Tailwind v4 `@theme` utilities cover all STATIC styling. But a handful of
 * leaf components and every SVG chart need a color computed at runtime from a
 * *dynamic* token (which detector? which regime? which confidence tier?) at a
 * specific opacity — e.g. a detector badge background at 9% of `--det-flow`.
 * That can't be a static utility class (Tailwind's JIT can't see dynamic class
 * names) and shouldn't be a per-(color × percent) token explosion. This single
 * typed helper is the sanctioned escape hatch — used everywhere a tinted,
 * token-driven color is needed, so the exact design percentages live in one place.
 */

/** A CSS custom-property name, e.g. `--det-flow`, `--win`, `--acc`. */
export type ColorToken = `--${string}`;

/** `var(--token)` — a direct reference to a design token (for SVG stroke/fill,
 *  solid backgrounds, text color). */
export function cssVar(token: ColorToken): string {
  return `var(${token})`;
}

/**
 * `color-mix(in oklab, var(--token) <pct>%, <base>)` — a tinted color derived
 * from a design token at the EXACT percentage the design specifies. `base`
 * defaults to `transparent` (the prototype's soft-fill convention).
 *
 * @param token  design-token name, e.g. `--det-flow`
 * @param pct    opacity percentage 0–100 (the design's exact value, e.g. 9, 14, 35)
 * @param base   what to mix toward (default `transparent`); pass a token via
 *               `cssVar(...)` to mix two tokens (e.g. tint over a surface).
 */
export function colorMix(token: ColorToken, pct: number, base = 'transparent'): string {
  return `color-mix(in oklab, var(${token}) ${pct}%, ${base})`;
}
