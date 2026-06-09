import type { TrackRecordItem } from '../../api/types';

/**
 * Realized directional return for an outcome row, as a signed percent.
 *
 * MARKET (composite) rows carry `composite_return_pct` (already a signed
 * fraction in the AI's direction) — use it directly. Single-asset rows
 * compute `(end - entry) / entry`, signed by the trade direction (a short
 * profits when price falls), where `end` is the validity-end close (falling
 * back to the 72h close for legacy rows). Returns null when the inputs to
 * compute a return aren't present.
 */
export function realizedReturnPct(item: TrackRecordItem): number | null {
  if (item.composite_return_pct !== null) return item.composite_return_pct * 100;
  if (item.entry_price === null) return null;
  const end = item.price_at_validity_end ?? item.price_after_72h;
  if (end === null) return null;
  const raw = (end - item.entry_price) / item.entry_price;
  const dir = item.direction.toLowerCase().includes('short') ? -1 : 1;
  return raw * dir * 100;
}

/** Directional single-asset return from entry to a given close, signed %. */
function singleAssetReturnPct(
  item: TrackRecordItem,
  end: number | null,
): number | null {
  if (item.entry_price === null || end === null) return null;
  const dir = item.direction.toLowerCase().includes('short') ? -1 : 1;
  return ((end - item.entry_price) / item.entry_price) * dir * 100;
}

/** +24h return, signed %. Null for MARKET (composite) rows — they carry no
 *  24h checkpoint — and for rows missing the 24h close. */
export function return24hPct(item: TrackRecordItem): number | null {
  if (item.composite_return_pct !== null) return null;
  return singleAssetReturnPct(item, item.price_after_24h);
}

/** +72h return, signed %. MARKET rows use the composite return. */
export function return72hPct(item: TrackRecordItem): number | null {
  if (item.composite_return_pct !== null) return item.composite_return_pct * 100;
  return singleAssetReturnPct(item, item.price_after_72h);
}

/** Horizon bucket label derived from the outcome's scoring window. */
export function horizonLabel(windowHours: number | null): string {
  if (windowHours === null) return 'legacy';
  if (windowHours <= 24) return 'scalp';
  if (windowHours <= 72) return 'swing';
  return 'position';
}
