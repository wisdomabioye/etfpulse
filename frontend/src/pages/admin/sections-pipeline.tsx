/**
 * Admin pipeline mutation sections (#187 split).
 *
 * The 4 sections that wrap existing simple-mutation hooks
 * (`TriggerCycleSection`, `RetryAiSection`, `EvalOutcomesSection`,
 * `RotateWebhookSecretSection`). They predate #186, are exercised through
 * `ActionsPanel` rendering, and have no test that needs to mount them in
 * isolation. Exported here so the `sections.tsx` composer can import them.
 */

import {
  useEvalOutcomes,
  useRetryAiNullSignals,
  useRotateWebhookSecret,
  useTriggerSignalCycle,
} from '../../api/queries';
import { ActionSection, ConfirmButton } from '../../components/admin';
import { Button } from '../../components/ui';
import {
  EvalOutcomesResultDisplay,
  RetryAiResultDisplay,
  RotateResult,
  TriggerResult,
} from './results';

// ===========================================================================
// Existing 4 simple-mutation sections (cycle, retry-ai, eval, rotate).
// ===========================================================================

export function TriggerCycleSection({ adminKey }: { adminKey: string }) {
  const m = useTriggerSignalCycle(adminKey);
  return (
    <ActionSection
      withDivider={false}
      title="Trigger signal cycle"
      description="Runs the same code path as the scheduled cron — ingest flows + news, run all 5 detectors, enrich with AI. Synchronous; may take ~10–60s."
      controls={
        <Button type="button" variant="primary" onClick={() => m.mutate()} disabled={m.isPending}>
          {m.isPending ? 'Running…' : 'Run cycle'}
        </Button>
      }
      error={m.error}
    >
      {m.data && <TriggerResult result={m.data} />}
    </ActionSection>
  );
}

export function RetryAiSection({ adminKey }: { adminKey: string }) {
  const m = useRetryAiNullSignals(adminKey);
  return (
    <ActionSection
      title="Retry AI on stale signals"
      description={
        <>
          Re-runs OpenRouter on Signals with NULL{' '}
          <code className="font-mono">ai_analysis</code> (stranded by an earlier
          credit-out / quota / schema failure — the daily cycle never retries existing
          rows). Caps at 10 calls per click.
        </>
      }
      controls={
        <Button
          type="button"
          variant="primary"
          onClick={() => m.mutate(10)}
          disabled={m.isPending}
        >
          {m.isPending ? 'Retrying…' : 'Retry AI (10)'}
        </Button>
      }
      error={m.error}
    >
      {m.data && <RetryAiResultDisplay result={m.data} />}
    </ActionSection>
  );
}

export function EvalOutcomesSection({ adminKey }: { adminKey: string }) {
  const m = useEvalOutcomes(adminKey);
  return (
    <ActionSection
      title="Evaluate outcomes now"
      description={
        <>
          Scores aged signals (≥72h old, with price + confidence) into{' '}
          <code className="font-mono">SignalOutcome</code> rows. The scheduled job runs
          hourly capped at <code className="font-mono">OUTCOME_EVAL_BATCH_LIMIT</code>{' '}
          (default 50); this button uses a smaller per-click cap of 20 to stay safely
          below SoSoValue's rate limit. Re-fire while{' '}
          <code className="font-mono">remaining &gt; 0</code> to drain a backlog.
        </>
      }
      controls={
        <Button
          type="button"
          variant="primary"
          onClick={() => m.mutate(20)}
          disabled={m.isPending}
        >
          {m.isPending ? 'Evaluating…' : 'Evaluate (20)'}
        </Button>
      }
      error={m.error}
    >
      {m.data && <EvalOutcomesResultDisplay result={m.data} />}
    </ActionSection>
  );
}

export function RotateWebhookSecretSection({ adminKey }: { adminKey: string }) {
  const m = useRotateWebhookSecret(adminKey);
  return (
    <ActionSection
      title="Rotate Telegram webhook secret"
      description={
        <>
          Issues a fresh secret and pushes it to Telegram race-free. Mirror the new
          value into <code className="font-mono">TELEGRAM_WEBHOOK_SECRET</code> before
          the next container restart, or the boot-time value will overwrite it.
        </>
      }
      controls={
        <ConfirmButton
          idleLabel="Rotate…"
          confirmLabel="Confirm rotate"
          onConfirm={() => m.mutate()}
          pending={m.isPending}
        />
      }
      error={m.error}
    >
      {m.data && <RotateResult result={m.data} />}
    </ActionSection>
  );
}
