/**
 * TypeScript mirrors of the backend Pydantic DTOs.
 *
 * Source of truth: etfpulse/backend/etfpulse/api/schemas/{signals,dashboard}.py
 * Update both when the API contract changes. Auto-generation from OpenAPI is
 * a future improvement; for ~4 endpoints with stable shapes the manual mirror
 * is faster to read in code review than a generated blob.
 *
 * Barrel module — the established `import type { Foo } from '../api/types'`
 * path stays valid for every consumer. The actual definitions live in
 * per-feature `types-*.ts` files (split to keep each under the size cap).
 */
export type * from './types-signals';
export type * from './types-track-record';
export type * from './types-regime';
export type * from './types-system';
