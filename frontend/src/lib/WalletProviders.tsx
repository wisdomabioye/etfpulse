/**
 * Wagmi provider wrapper — mounted ONLY on routes that need wallet hooks
 * (`/login`, `/execute`). Imported via React.lazy in App.tsx so the
 * 1.5 MB AppKit + wagmi + viem bundle is code-split out of the public
 * page chunk (#78.5).
 *
 * Importing this module pulls `../lib/wagmi`, which transitively pulls:
 *   - wagmi core + viem chains
 *   - @reown/appkit + @reown/appkit-adapter-wagmi
 *   - secp256k1 / ws / various sub-deps
 *
 * Public pages (Home / Signals / TrackRecord / Regime / Analytics) do
 * NOT import this — they cost nothing in initial bundle. Users who
 * never tap Trade get the slim path.
 *
 * AppKit's `createAppKit` side-effect-runs once at module-eval (gated
 * by the #78.6 HMR singleton flag), so the modal is registered
 * globally the first time ANY wallet route mounts and stays
 * registered for the rest of the session.
 *
 * Default-exported so `React.lazy(() => import('./WalletProviders'))`
 * works without a `.then(m => ({ default: m.X }))` shim.
 */

import type { ReactNode } from 'react';
import { WagmiProvider } from 'wagmi';

import { wagmiConfig } from './wagmi';

interface Props {
  children: ReactNode;
}

export function WalletProviders({ children }: Props) {
  return <WagmiProvider config={wagmiConfig}>{children}</WagmiProvider>;
}

export default WalletProviders;
