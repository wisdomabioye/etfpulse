/**
 * Wallet-state gates for the Execute page:
 *   - WalletMissingNotice / *Unavailable / *WithWallet — the inline SIWE
 *     onboarding shown to a Telegram-WebApp user with a JWT but no bound
 *     wallet (D.5 bind-to-existing flow).
 *
 * Rendered inside the /execute route's WagmiProvider, so wagmi hooks are safe.
 */

import { useRef, useState } from 'react';
import { useAccount, useSignMessage } from 'wagmi';
import { useAppKit } from '@reown/appkit/react';
import { useQueryClient } from '@tanstack/react-query';

import { performSiweLogin } from '../../auth/siwe';
import { useAuth } from '../../auth/useAuth';
import { KEY_WALLET_ME } from '../../hooks/useExecution';
import { isWalletConnectAvailable } from '../../lib/wagmi';
import { Button, Card } from '../ui';

// Exported for #78.4 regression test (also re-exported from pages/Execute).
export function WalletMissingNotice() {
  // Telegram-WebApp users land here with a JWT but `wallet_address=null`.
  // Run SIWE inline — the backend (`/api/wallet/verify`) honors the inbound
  // Authorization header and BINDS the wallet to the existing user (D.5).
  //
  // `useAppKit()` THROWS at render time if `createAppKit` was never called
  // (no `VITE_WALLETCONNECT_PROJECT_ID`). Render the conditional BEFORE
  // calling AppKit hooks — same pattern as <Login>.
  if (!isWalletConnectAvailable) {
    return <WalletMissingUnavailable />;
  }
  return <WalletMissingWithWallet />;
}

export function WalletMissingUnavailable() {
  return (
    <section className="rounded-xl border border-amber-500/30 bg-amber-500/5 p-4 space-y-2">
      <h2 className="text-lg font-semibold text-amber-200">Wallet not bound</h2>
      <p className="text-t2 text-sm">
        Wallet Connect isn&apos;t configured on this deployment, so wallet binding can&apos;t
        run here. The site administrator must set{' '}
        <code className="text-t1">VITE_WALLETCONNECT_PROJECT_ID</code> and redeploy. Until
        then, trading is disabled.
      </p>
    </section>
  );
}

// The three onboarding steps shown to a not-yet-bound wallet, ported from
// the prototype's `ExecuteOnboarding`.
const ONBOARDING_STEPS = [
  {
    n: '01',
    t: 'Connect wallet',
    d: 'Sign in with Ethereum (SIWE) or open via Telegram. We read your address — never your keys.',
  },
  {
    n: '02',
    t: 'Bind SoDEX API key',
    d: 'Auto-discovered from your wallet, or paste manually. Scoped to trading only.',
  },
  {
    n: '03',
    t: 'Trade in paper first',
    d: 'Every new account starts in paper mode. Request live trading when you’re ready.',
  },
];

function WalletMissingWithWallet() {
  // Safe to call AppKit hooks unconditionally — this component only mounts
  // when `isWalletConnectAvailable` is true (verified by the parent).
  const { login, jwt } = useAuth();
  const { address, isConnected } = useAccount();
  const { open } = useAppKit();
  const { signMessageAsync } = useSignMessage();
  const qc = useQueryClient();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Synchronous in-flight guard. `busy` is React state — a fast double-click
  // would slip past `disabled` and fire `handleSign` twice (two nonces, two
  // wallet prompts). Same fix as D.4.5 Login.
  const inFlightRef = useRef(false);

  async function handleConnect() {
    setError(null);
    try {
      await open();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to open wallet picker.');
    }
  }

  async function handleSign() {
    if (!address) return;
    if (inFlightRef.current) return;
    inFlightRef.current = true;
    setBusy(true);
    setError(null);
    try {
      // performSiweLogin uses apiPost, which auto-attaches the current JWT as
      // Authorization. Backend route detects it and binds the verified wallet
      // to the existing User row.
      const resp = await performSiweLogin({ address, signMessageAsync });
      login(resp.jwt);
      // /me must refetch — user.wallet_address just flipped null → bound.
      qc.invalidateQueries({ queryKey: KEY_WALLET_ME });
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      if (/rejected|denied/i.test(msg)) {
        setError('Signature rejected. Tap "Sign in" to retry.');
      } else {
        setError(msg);
      }
    } finally {
      inFlightRef.current = false;
      setBusy(false);
    }
  }

  // Defensive — should never see this if not authed (parent <Execute>
  // redirects), but the JWT must be present for the bind-to-existing path.
  if (!jwt) return null;

  return (
    <Card className="max-w-[520px] mx-auto text-center" pad>
      <div
        className="w-[52px] h-[52px] rounded-md bg-acc-soft border border-acc-line flex items-center justify-center mx-auto mb-5"
        aria-hidden
      >
        <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="var(--acc)" strokeWidth="1.8">
          <rect x="2" y="6" width="20" height="13" rx="2" />
          <path d="M2 10h20M16 14h2" />
        </svg>
      </div>
      <h2 className="text-[22px] font-semibold tracking-[-0.02em] mb-2.5">
        Connect a wallet to trade
      </h2>
      <p className="text-t2 text-[14px] leading-[1.55] mb-7">
        You&apos;re signed in via Telegram, but no SoDEX-trading wallet is connected yet.
        Execution is non-custodial — you sign every entry, stop, take-profit, and close in
        your own wallet. ETFPulse never trades for you.
      </p>
      <div className="flex flex-col gap-2.5 mb-6 text-left">
        {ONBOARDING_STEPS.map((s) => (
          <div
            key={s.n}
            className="grid grid-cols-[auto_1fr] gap-3.5 px-4 py-3.5 bg-bg-2 border border-line-2 rounded-md"
          >
            <span className="font-mono text-[11px] text-acc">{s.n}</span>
            <div>
              <div className="text-[14px] font-semibold mb-[3px]">{s.t}</div>
              <div className="text-[12.5px] text-t3 leading-[1.5]">{s.d}</div>
            </div>
          </div>
        ))}
      </div>
      <div className="flex gap-2.5 justify-center">
        {!isConnected ? (
          <Button variant="primary" size="lg" onClick={handleConnect}>
            Connect Wallet
          </Button>
        ) : (
          <Button variant="primary" size="lg" onClick={handleSign} disabled={busy}>
            {busy ? 'Waiting for signature…' : 'Sign in with Ethereum'}
          </Button>
        )}
      </div>
      {isConnected && (
        <div className="mt-3 text-[11px] text-t3 break-all">
          Connected as <code className="text-t1">{address}</code>
        </div>
      )}
      {error && (
        <div className="mt-3 p-3 rounded-md border border-loss/30 bg-loss-soft text-[12.5px] text-loss text-left">
          {error}
        </div>
      )}
    </Card>
  );
}
