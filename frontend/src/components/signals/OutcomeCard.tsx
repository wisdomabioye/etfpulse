import type { SignalOutcome } from '../../api/types';

interface OutcomeCardProps {
  outcome: SignalOutcome | null;
  /** ISO datetime when the signal expires — used to compute "evaluates in Xh". */
  expiresAt: string | null;
}

/**
 * Outcome section of the signal detail page.
 *
 * Wave 1: outcome is always null (Stage 08 / open_issues.md #34 blocks
 * outcome evaluation). Renders a dashed-border "Pending" placeholder
 * with a countdown derived from `expires_at`.
 *
 * Wave 2+: when outcome rows arrive, swap the placeholder for a real
 * readout — entry/stop/target on the left, 24h/72h prices on the right,
 * hit/miss indicator up top. Only the `outcome !== null` branch needs to
 * grow; the pending branch stays as a fallback.
 */
export function OutcomeCard({ outcome, expiresAt }: OutcomeCardProps) {
  if (outcome === null) {
    const countdown = expiresAt ? formatCountdown(expiresAt) : null;
    return (
      <div
        className="px-5 py-5 rounded-lg bg-bg-2 text-center font-mono text-[13px] text-text-3"
        style={{ border: '1px dashed var(--color-border-3)' }}
      >
        Pending{countdown ? ` · evaluates in ${countdown}` : ''}
      </div>
    );
  }

  // Evaluated — Wave 2+. Minimal display now so Stage 08 can flesh out.
  return (
    <div className="border border-border-2 rounded-lg bg-bg-2 p-5">
      <div className="grid grid-cols-3 gap-4 font-mono text-[12px]">
        <Metric label="Price at signal" value={formatUsd(outcome.price_at_signal)} />
        <Metric
          label="Price +24h"
          value={outcome.price_after_24h !== null ? formatUsd(outcome.price_after_24h) : '—'}
        />
        <Metric
          label="Price +72h"
          value={outcome.price_after_72h !== null ? formatUsd(outcome.price_after_72h) : '—'}
        />
      </div>
    </div>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-[10px] text-text-3 uppercase tracking-[0.1em] mb-1">{label}</div>
      <div className="text-[14px] text-text-1 tabular-nums">{value}</div>
    </div>
  );
}

function formatUsd(n: number): string {
  return `$${n.toLocaleString('en-US', { maximumFractionDigits: 2 })}`;
}

function formatCountdown(iso: string): string | null {
  const target = new Date(iso).getTime();
  if (isNaN(target)) return null;
  const diffMs = target - Date.now();
  if (diffMs <= 0) return 'due now';
  const hours = Math.floor(diffMs / 3_600_000);
  if (hours < 1) return '<1h';
  if (hours < 48) return `${hours}h`;
  const days = Math.floor(hours / 24);
  return `${days}d`;
}
