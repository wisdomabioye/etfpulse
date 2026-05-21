/**
 * Wallet-connect + SIWE login page.
 *
 * Three states the page flips between:
 *
 *   1. Wallet not connected — show "Connect Wallet" CTA that opens
 *      Reown AppKit modal.
 *   2. Wallet connected, no JWT — show "Sign in with Ethereum" CTA
 *      that runs the SIWE ceremony.
 *   3. Authed — redirect to /execute.
 *
 * Component split: `<Login>` checks `isWalletConnectAvailable` and
 * either renders `<LoginUnavailable>` OR `<LoginWithWallet>`. The
 * split is load-bearing: `useAppKit()` THROWS if `createAppKit` was
 * never called (no projectId path in `wagmi.ts`). Without the split,
 * the no-projectId deployment would crash the entire /login route
 * before any fallback UI can render. Rules of Hooks forbids
 * conditional hook calls inside one component; the standard pattern
 * is conditional rendering of two components, each of which calls
 * hooks unconditionally.
 *
 * Already-authed visitors get bounced immediately so the page isn't
 * sticky after a previous session.
 */

import { useRef, useState } from 'react';
import { Navigate } from 'react-router-dom';
import { useAccount, useSignMessage } from 'wagmi';
import { useAppKit } from '@reown/appkit/react';

import { performSiweLogin } from '../auth/siwe';
import { useAuth } from '../auth/useAuth';
import { isWalletConnectAvailable } from '../lib/wagmi';

export function Login() {
  const { isAuthed } = useAuth();
  if (isAuthed) {
    // Already logged in — bounce to the Execute page. `replace` so
    // back-button doesn't re-show /login after a refresh.
    return <Navigate to="/execute" replace />;
  }
  if (!isWalletConnectAvailable) {
    // Deployment didn't set VITE_WALLETCONNECT_PROJECT_ID; AppKit isn't
    // initialised and `useAppKit()` would throw. Render the static
    // fallback that explains what to fix without calling the hook.
    return <LoginUnavailable />;
  }
  return <LoginWithWallet />;
}

function LoginUnavailable() {
  return (
    <div className="max-w-md mx-auto px-6 py-12">
      <h1 className="text-2xl font-semibold mb-2">Sign in unavailable</h1>
      <p className="text-text-2 mb-8">
        Wallet Connect isn&apos;t configured on this deployment. The site administrator must set
        <code className="mx-1 text-text-1">VITE_WALLETCONNECT_PROJECT_ID</code>
        and redeploy. Get a free project ID at{' '}
        <a className="text-accent-1 underline" href="https://cloud.reown.com">
          cloud.reown.com
        </a>
        .
      </p>
    </div>
  );
}

function LoginWithWallet() {
  // Safe to call unconditionally — this component only mounts when
  // `isWalletConnectAvailable` is true, i.e. `createAppKit` did run.
  const { login } = useAuth();
  const { address, isConnected } = useAccount();
  const { open } = useAppKit();
  const { signMessageAsync } = useSignMessage();

  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Synchronous in-flight guard. `busy` is React state — `setBusy(true)`
  // doesn't update the DOM until next paint, so a fast double-click
  // would slip past the `disabled` prop and fire `handleSiwe` twice
  // (two nonces, two wallet sign prompts). A ref check at handler entry
  // closes that window because refs are read/written synchronously.
  const inFlightRef = useRef(false);

  // After connecting, we DO NOT auto-trigger the SIWE signature prompt.
  // Surprise sign-prompts erode trust. The user clicks Connect → wallet
  // picker → connect → THEN clicks "Sign in" → wallet sign prompt. Two
  // explicit user-initiated steps. The stale-error case (switching
  // accounts without retrying) is intentionally not auto-cleared —
  // any new action via handleConnect/handleSiwe resets error first.

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
      // useAuth's isAuthed flips → parent <Login> returns <Navigate to="/execute">.
    } catch (e) {
      // Distinguish user-rejected (common, expected) from genuine
      // errors. wagmi/viem throws a `UserRejectedRequestError` with
      // a stable message substring on user reject.
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
    <div className="max-w-md mx-auto px-6 py-12">
      <h1 className="text-2xl font-semibold mb-2">Sign in</h1>
      <p className="text-text-2 mb-8">
        Connect your wallet and sign a message to start trading on SoDEX. We never hold private
        keys — every order is signed in your wallet.
      </p>

      {!isConnected && (
        <button
          type="button"
          onClick={handleConnect}
          className="w-full px-4 py-3 rounded-lg bg-accent-1 text-bg-0 font-medium"
        >
          Connect Wallet
        </button>
      )}

      {isConnected && (
        <div className="space-y-4">
          <div className="text-sm text-text-2">
            Connected as <code className="text-text-1">{address}</code>
          </div>
          <button
            type="button"
            onClick={handleSiwe}
            disabled={busy || !address}
            className="w-full px-4 py-3 rounded-lg bg-accent-1 text-bg-0 font-medium disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {busy ? 'Waiting for signature…' : 'Sign in with Ethereum'}
          </button>
        </div>
      )}

      {error && (
        <div className="mt-6 p-4 rounded-lg border border-red-500/30 bg-red-500/10 text-sm text-red-300">
          {error}
        </div>
      )}
    </div>
  );
}
