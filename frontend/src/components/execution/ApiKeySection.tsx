/**
 * ApiKeySection — replaces the manual `api_key_name + sodex_account_id`
 * form with an auto-fetch flow against `/api/wallet/sodex-bootstrap`.
 *
 * Render states (in priority order):
 *
 *   1. Wallet not bound yet → returns null. The parent surface
 *      (Execute page's `WalletMissingNotice`) owns that messaging;
 *      this section has nothing useful to say without an address.
 *   2. All venues bound already → returns null.
 *   3. Bootstrap query loading → loading shell.
 *   4. Bootstrap query errored → "SoDEX bootstrap unavailable" notice.
 *      There is intentionally NO manual-form fallback: the manual
 *      form needed `account_id` typed by the user, but `account_id`
 *      itself comes from the same SoDEX endpoint that just failed
 *      — so re-asking the operator wouldn't unblock them. They
 *      need to retry once SoDEX recovers.
 *   5. account_id is null on the gateway → "create your SoDEX account"
 *      guidance.
 *   6. Per missing venue:
 *      - 0 keys → "register a key" guidance.
 *      - 1 key  → auto-bind via useSetApiKey, fires ONCE per session
 *        (useRef guard); shows a one-line confirmation.
 *      - 2+ keys → dropdown of names; one click to bind.
 *
 * Pure-FE; no schema migration; existing `useSetApiKey` mutation
 * unchanged. Each venue resolves independently — partial states
 * (1 spot key + 2 perps keys) are first-class.
 */

import { useEffect, useRef, useState } from 'react';

import type { APIKeyOut, Venue, WalletMeResponse } from '../../api/execution';
import { useSetApiKey, useSodexBootstrap } from '../../hooks/useExecution';

interface ApiKeySectionProps {
  me: WalletMeResponse;
}

export function ApiKeySection({ me }: ApiKeySectionProps) {
  // Wallet-less users (e.g. Telegram-WebApp path before SIWE bind)
  // have nothing for SoDEX to look up. The Execute page already
  // surfaces `<WalletMissingNotice>` for that state — duplicating
  // a "no SoDEX account" message here would be a confusing
  // false-flag. Short-circuit so the rest of the page stays clean.
  if (!me.wallet_address) return null;

  const missing: Venue[] = [];
  if (!me.sodex_spot_api_key_name) missing.push('sodex_spot');
  if (!me.sodex_perps_api_key_name) missing.push('sodex_perps');
  if (missing.length === 0) return null;

  return <ApiKeyResolver me={me} missing={missing} />;
}

function ApiKeyResolver({ me, missing }: { me: WalletMeResponse; missing: Venue[] }) {
  const bootstrap = useSodexBootstrap(me.wallet_address);

  if (bootstrap.isLoading) {
    return <ApiKeyShell tone="info" title="Reading SoDEX account…"><LoadingBars /></ApiKeyShell>;
  }
  if (bootstrap.isError) {
    return <ApiKeyBootstrapUnavailable />;
  }
  const data = bootstrap.data;
  if (data == null) {
    return <ApiKeyBootstrapUnavailable />;
  }
  if (data.account_id === null) {
    return <ApiKeyNoAccount />;
  }

  return (
    <section className="space-y-3">
      {missing.map((venue) => (
        <ApiKeyVenuePanel
          key={venue}
          venue={venue}
          accountId={data.account_id as number}
          keys={venue === 'sodex_spot' ? data.spot_keys : data.perps_keys}
        />
      ))}
    </section>
  );
}

/* ─────────────────────────── per-venue branching ─────────────────────────── */

interface ApiKeyVenuePanelProps {
  venue: Venue;
  accountId: number;
  keys: APIKeyOut[];
}

function ApiKeyVenuePanel({ venue, accountId, keys }: ApiKeyVenuePanelProps) {
  if (keys.length === 0) return <ApiKeyRegisterPrompt venue={venue} />;
  if (keys.length === 1)
    return (
      <ApiKeyAutoBind venue={venue} accountId={accountId} keyName={keys[0].name} />
    );
  return <ApiKeyMultiSelect venue={venue} accountId={accountId} keys={keys} />;
}

/* ─────────────────────────── single-key auto-bind ─────────────────────────── */

function ApiKeyAutoBind({
  venue,
  accountId,
  keyName,
}: {
  venue: Venue;
  accountId: number;
  keyName: string;
}) {
  const setKey = useSetApiKey();
  // useRef guard — TanStack returns a fresh mutation-result object on
  // every render, so depending on `setKey` directly would re-run the
  // effect each render. We depend on `setKey.mutate` (a stable
  // function reference in TanStack v5) and keep the ref guard as a
  // belt-and-braces against any future identity drift.
  const firedRef = useRef(false);
  const { mutate } = setKey;
  useEffect(() => {
    if (firedRef.current) return;
    firedRef.current = true;
    mutate({ venue, api_key_name: keyName, sodex_account_id: accountId });
  }, [venue, keyName, accountId, mutate]);

  return (
    <ApiKeyShell tone="pos" title={`Binding ${venueLabel(venue)} · ${keyName}`}>
      <p className="text-t2 text-sm">
        Found one registered API key for {venueLabel(venue)} — binding
        automatically. The page refreshes once the key is saved.
      </p>
      {setKey.isError && (
        <p className="text-[12px] text-loss mt-2">
          Could not bind:{' '}
          {setKey.error instanceof Error
            ? setKey.error.message
            : 'unknown error — see server logs'}
        </p>
      )}
    </ApiKeyShell>
  );
}

/* ─────────────────────────── multi-key dropdown ─────────────────────────── */

function ApiKeyMultiSelect({
  venue,
  accountId,
  keys,
}: {
  venue: Venue;
  accountId: number;
  keys: APIKeyOut[];
}) {
  const setKey = useSetApiKey();
  const [selected, setSelected] = useState<string>(keys[0].name);

  return (
    <ApiKeyShell tone="warn" title={`Choose your ${venueLabel(venue)} API key`}>
      <p className="text-t2 text-sm mb-3">
        Multiple named API keys are registered for this wallet on{' '}
        {venueLabel(venue)}. Pick the one ETFPulse should sign with.
      </p>
      <div className="flex flex-wrap items-center gap-3">
        <select
          value={selected}
          onChange={(e) => setSelected(e.target.value)}
          className="px-3 py-2 rounded-lg bg-bg-0 border border-line-2 text-sm"
          aria-label={`${venueLabel(venue)} API key`}
        >
          {keys.map((k) => (
            <option key={k.name} value={k.name}>
              {k.name}
            </option>
          ))}
        </select>
        <button
          type="button"
          disabled={setKey.isPending}
          onClick={() =>
            setKey.mutate({
              venue,
              api_key_name: selected,
              sodex_account_id: accountId,
            })
          }
          className="px-4 py-2 rounded-lg bg-acc text-bg-0 font-medium disabled:opacity-50"
        >
          {setKey.isPending ? 'Saving…' : 'Bind key'}
        </button>
      </div>
      {setKey.isError && (
        <p className="text-[12px] text-loss mt-2">
          Could not bind:{' '}
          {setKey.error instanceof Error
            ? setKey.error.message
            : 'unknown error — see server logs'}
        </p>
      )}
    </ApiKeyShell>
  );
}

/* ─────────────────────────── empty + error states ─────────────────────────── */

function ApiKeyRegisterPrompt({ venue }: { venue: Venue }) {
  return (
    <ApiKeyShell tone="warn" title={`No ${venueLabel(venue)} API key registered`}>
      <p className="text-t2 text-sm">
        Open the SoDEX {venueLabel(venue)} dashboard, register a named
        API key for this wallet, then refresh this page. ETFPulse signs
        every order in your wallet — the registered key only lets the
        gateway look up the signer.
      </p>
    </ApiKeyShell>
  );
}

function ApiKeyNoAccount() {
  return (
    <ApiKeyShell tone="warn" title="No SoDEX account for this wallet">
      <p className="text-t2 text-sm">
        SoDEX doesn&apos;t recognise this wallet. Visit the SoDEX dashboard
        once to create your account (it&apos;s automatic — the first
        interaction provisions it), then refresh this page.
      </p>
    </ApiKeyShell>
  );
}

function ApiKeyBootstrapUnavailable() {
  return (
    <ApiKeyShell tone="warn" title="SoDEX bootstrap unavailable">
      <p className="text-t2 text-sm">
        Could not auto-discover your API keys. The SoDEX gateway may be
        flaky right now — refresh in a moment, or contact the ETFPulse
        operator if the issue persists.
      </p>
    </ApiKeyShell>
  );
}

/* ─────────────────────────── primitives ─────────────────────────── */

type Tone = 'pos' | 'warn' | 'info';

const TONE_CLASS: Record<Tone, string> = {
  pos: 'border-emerald-500/30 bg-emerald-500/5',
  warn: 'border-amber-500/30 bg-amber-500/5',
  info: 'border-line-2 bg-bg-2',
};

const TONE_TITLE: Record<Tone, string> = {
  pos: 'text-emerald-200',
  warn: 'text-amber-200',
  info: 'text-t1',
};

function ApiKeyShell({
  tone,
  title,
  children,
}: {
  tone: Tone;
  title: string;
  children: React.ReactNode;
}) {
  return (
    <section className={`rounded-xl border p-4 space-y-2 ${TONE_CLASS[tone]}`}>
      <h2 className={`text-lg font-semibold ${TONE_TITLE[tone]}`}>{title}</h2>
      {children}
    </section>
  );
}

function LoadingBars() {
  return (
    <div className="space-y-2" aria-hidden>
      <div className="h-2 w-2/3 bg-bg-3 rounded animate-pulse" />
      <div className="h-2 w-1/2 bg-bg-3 rounded animate-pulse" />
    </div>
  );
}

function venueLabel(v: Venue): string {
  return v === 'sodex_spot' ? 'Spot' : 'Perps';
}
