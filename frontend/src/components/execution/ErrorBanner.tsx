import { formatError } from './execErrors';

/** Loss-toned inline error banner used across the execution surface. */
export function ErrorBanner({ error, fallback }: { error: unknown; fallback: string }) {
  return (
    <div className="p-3 rounded-md border border-loss/30 bg-loss-soft text-sm text-loss">
      {formatError(error) || fallback}
    </div>
  );
}
