/**
 * `<ActionSection>` — one operator-action row in `Admin.tsx` (#187).
 *
 * Layout contract: title + description on the left (flex-1, wraps), controls
 * on the right (no-shrink), error + result rendered as siblings below (so
 * they stack full-width regardless of the controls' width).
 *
 * Why a Fragment, not a wrapping div: the parent `<section>` uses
 * `space-y-4` to vertically space items. Wrapping in another div would
 * require the parent to nest its spacing, breaking the existing visual
 * rhythm. Three sibling children gets the spacing for free.
 *
 * `withDivider` defaults true; the FIRST section in a list sets false to
 * skip the top border. Mirrors the existing inline pattern.
 *
 * `children` is the result-display slot — each section owns its own
 * result shape (`TriggerResult`, `HaltResult`, etc.) so the primitive
 * stays free of result-rendering coupling.
 */

import type { ReactNode } from 'react';

import { ApiError } from '../../api/client';
import { Callout } from '../ui';

interface Props {
  title: ReactNode;
  description: ReactNode;
  controls: ReactNode;
  /** Mutation/query error. `null`/`undefined` → no error block rendered. */
  error?: unknown;
  /** Default true. First section in a list sets false. */
  withDivider?: boolean;
  /** Result-display content (Callout, table, etc.). Section owns the shape. */
  children?: ReactNode;
}

export function ActionSection({
  title,
  description,
  controls,
  error,
  withDivider = true,
  children,
}: Props) {
  return (
    <>
      <div
        className={
          'flex flex-wrap items-start gap-3' +
          (withDivider ? ' border-t border-border-2 pt-4' : '')
        }
      >
        <div className="flex-1 min-w-[260px]">
          <div className="text-[14px] font-semibold text-text-1">{title}</div>
          <div className="text-[12px] text-text-3">{description}</div>
        </div>
        {controls}
      </div>
      {error != null && <ActionErrorInline error={error} />}
      {children}
    </>
  );
}

/** Internal error renderer. Accepts `unknown` so callers can pass
 *  `mutation.error` (typed as `Error | null`) or `query.error` without
 *  type gymnastics. Narrows to `ApiError` via `instanceof` to render
 *  the operator-friendly `HTTP status · detail` shape; falls back to
 *  `.message` for plain `Error`s and `String(error)` for everything
 *  else. The explicit `ApiError` import is intentional — this primitive
 *  is admin-named + admin-only, so the api-layer dependency is honest. */
function ActionErrorInline({ error }: { error: unknown }) {
  let detail: string;
  if (error instanceof ApiError) {
    detail = `HTTP ${error.status} · ${error.detail}`;
  } else if (error instanceof Error) {
    detail = error.message;
  } else {
    detail = String(error);
  }
  return (
    <Callout tone="neg">
      <span className="font-mono text-[12px]">{detail}</span>
    </Callout>
  );
}
