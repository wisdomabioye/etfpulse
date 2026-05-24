/**
 * Parse a non-empty digit string into a positive int, else null.
 *
 * Used by every admin section that takes an id input (user_id, signal_id,
 * group_id) — keeps the empty-string-vs-NaN guard in one place.
 *
 * Why this specific shape:
 *   - Strict `/^\d+$/.test(trim)` rejects negatives, hex, scientific
 *     notation, whitespace-internal, etc. — `Number.parseInt` alone
 *     would silently accept `"42abc"` as 42.
 *   - `n > 0` rejects zero — id=0 is never a valid primary key and
 *     `session.get(User, 0)` always returns None on the backend.
 *   - Returns `null` (not a thrown error) so call sites can use it
 *     directly in conditional rendering / disabled-button gates.
 *
 * Lives in `lib/` not `pages/Admin.tsx` because react-refresh requires
 * page modules to only export components — co-locating this helper
 * would break HMR.
 */
export function parsePositiveId(raw: string): number | null {
  if (!/^\d+$/.test(raw.trim())) return null;
  const n = Number.parseInt(raw.trim(), 10);
  return n > 0 ? n : null;
}
