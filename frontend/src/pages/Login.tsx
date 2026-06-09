/**
 * Wallet-connect + SIWE login page (R-fix: ported to the prototype's centered
 * card treatment, REAL connect→sign logic preserved exactly).
 *
 * Three states the page flips between:
 *   1. Wallet not connected — "Connect Wallet" opens the AppKit modal.
 *   2. Wallet connected, no JWT — "Sign in with Ethereum" runs SIWE.
 *   3. Authed — redirect to /execute (or the bounced-from URL).
 *
 * The `<LoginUnavailable>` / `<LoginWithWallet>` split is load-bearing:
 * `useAppKit()` THROWS if `createAppKit` was never called (no-projectId
 * deploy). Conditional rendering of two components keeps Rules of Hooks.
 */

import type { ReactNode } from 'react';
import { useRef, useState } from 'react';
import { Navigate, useLocation } from 'react-router-dom';

import { resolvePostLoginPath } from '../auth/postLoginPath';
import { useAccount, useDisconnect, useSignMessage } from 'wagmi';
import { useAppKit } from '@reown/appkit/react';

import { performSiweLogin } from '../auth/siwe';
import { useAuth } from '../auth/useAuth';
import { Page } from '../components/layout';
import { Button, Card, Logo } from '../components/ui';
import { isWalletConnectAvailable } from '../lib/wagmi';
import { TELEGRAM_BOT_URL } from '../lib/links';

const WalletIcon = (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden>
    <path d="M21 12V7H5a2 2 0 010-4h14v4" />
    <path d="M3 5v14a2 2 0 002 2h16v-5" />
    <path d="M18 12a2 2 0 000 4h4v-4z" />
  </svg>
);

/** Centered logo + card shell — the prototype's login chrome. */
function LoginShell({ children }: { children: ReactNode }) {
  return (
    <Page>
      <div className="max-w-[420px] mx-auto my-[60px]">
        <div className="text-center mb-7 flex justify-center">
          <Logo size={20} />
        </div>
        <Card className="p-7">{children}</Card>
      </div>
    </Page>
  );
}

export function Login() {
  const { isAuthed } = useAuth();
  const location = useLocation();
  if (isAuthed) {
    return <Navigate to={resolvePostLoginPath(location)} replace />;
  }
  if (!isWalletConnectAvailable) {
    return <LoginUnavailable />;
  }
  return <LoginWithWallet />;
}

function LoginUnavailable() {
  return (
    <LoginShell>
      <h1 className="text-[22px] font-semibold tracking-[-0.02em] mb-1.5 text-center">
        Sign in unavailable
      </h1>
      <p className="text-t3 text-[13px] text-center leading-[1.5]">
        Wallet Connect isn&apos;t configured on this deployment. The administrator must set
        <code className="mx-1 text-t1">VITE_WALLETCONNECT_PROJECT_ID</code> and redeploy. Get a free
        project ID at{' '}
        <a className="text-acc underline" href="https://cloud.reown.com">
          cloud.reown.com
        </a>
        .
      </p>
    </LoginShell>
  );
}

function LoginWithWallet() {
  const { login } = useAuth();
  const { address, isConnected } = useAccount();
  const { open } = useAppKit();
  const { signMessageAsync } = useSignMessage();
  const { disconnect } = useDisconnect();

  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inFlightRef = useRef(false);

  async function handleConnect() {
    setError(null);
    try {
      await open();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to open wallet picker.');
    }
  }

  async function handleSiwe() {
    if (!address) return;
    if (inFlightRef.current) return; // synchronous double-click guard
    inFlightRef.current = true;
    setBusy(true);
    setError(null);
    try {
      const resp = await performSiweLogin({ address, signMessageAsync });
      login(resp.jwt);
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      if (/rejected|denied/i.test(msg)) {
        setError('Signature rejected. Click "Sign in" to retry.');
      } else {
        setError(msg);
      }
    } finally {
      inFlightRef.current = false;
      setBusy(false);
    }
  }

  return (
    <LoginShell>
      <h1 className="text-[22px] font-semibold tracking-[-0.02em] mb-1.5 text-center">
        Connect to ETFPulse
      </h1>
      <p className="text-t3 text-[13px] text-center mb-6 leading-[1.5]">
        Public data needs no login. Connect a wallet only to trade — non-custodial, you sign
        everything.
      </p>

      <div className="flex flex-col gap-2.5">
        {!isConnected ? (
          <Button variant="primary" size="lg" full icon={WalletIcon} onClick={handleConnect}>
            Connect Wallet
          </Button>
        ) : (
          <Button
            variant="primary"
            size="lg"
            full
            icon={WalletIcon}
            onClick={handleSiwe}
            disabled={busy || !address}
          >
            {busy ? 'Waiting for signature…' : 'Sign in with Ethereum'}
          </Button>
        )}
        <Button as="a" href={TELEGRAM_BOT_URL} target="_blank" rel="noopener noreferrer" variant="outline" size="lg" full>
          Open via Telegram
        </Button>
      </div>

      {isConnected && (
        <div className="flex items-center justify-between gap-3 text-[12px] text-t3 mt-3">
          <span className="truncate">
            Connected as <code className="text-t1">{address}</code>
          </span>
          <button
            type="button"
            onClick={() => {
              setError(null);
              disconnect();
            }}
            className="shrink-0 hover:text-t1 underline"
          >
            Disconnect
          </button>
        </div>
      )}

      <div className="flex items-center gap-3 my-[22px]">
        <span className="flex-1 h-px bg-line-2" />
        <span className="font-mono text-[10px] text-t4">SIWE · EIP-4361</span>
        <span className="flex-1 h-px bg-line-2" />
      </div>
      <p className="font-mono text-[10.5px] text-t4 text-center leading-[1.6]">
        By connecting you agree these are information signals, not financial advice. New accounts
        start in paper mode.
      </p>

      {error && (
        <div className="mt-6 p-4 rounded-lg border border-loss/30 bg-loss-soft text-[13px] text-loss">
          {error}
        </div>
      )}
    </LoginShell>
  );
}
