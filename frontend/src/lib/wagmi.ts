/**
 * wagmi config + Reown AppKit initialisation.
 *
 * SoDEX integration honesty disclaimer (see also `sodex-chains.ts`):
 * SoDEX is an off-chain order-routing gateway, NOT a blockchain. The
 * "chain" we register below is a wagmi/AppKit-required stub whose only
 * real attribute is the chainId — used as an EIP-712 domain separator
 * in `signTypedData`. Wallets may display the stub's name/native
 * currency in their network UI; that's wagmi's structural cost, not a
 * claim of substance.
 *
 * Why Reown AppKit (formerly Web3Modal): WalletConnect v2-native modal
 * with broad wallet coverage (MetaMask, Rainbow, Coinbase Wallet,
 * Trust, etc) with one hosted modal — no per-wallet connector wiring.
 * The alternative — hand-rolling a wallet picker — burns weeks for
 * something AppKit gives us for one project ID.
 *
 * Project ID:
 *   Get one free at https://cloud.reown.com. Set
 *   `VITE_WALLETCONNECT_PROJECT_ID` in `.env.local` (dev) or the
 *   deploy env (prod). When empty, this module logs a `console.warn`
 *   on each page load and exposes `isWalletConnectAvailable=false`
 *   so the Connect button can render a disabled state rather than
 *   crash.
 *
 * Chain registration:
 *   ONE chain only — the env-active stub. There's no concept of
 *   "switching" between SoDEX testnet and mainnet at runtime; the
 *   active env is fixed at deploy time via `VITE_SODEX_CHAIN_ID`.
 *   Registering both would surface a meaningless network-switch
 *   prompt in some wallets.
 */

import { WagmiAdapter } from '@reown/appkit-adapter-wagmi';
import { createAppKit } from '@reown/appkit/react';
import type { AppKitNetwork } from '@reown/appkit/networks';

import { getActiveSodexStubChain } from './sodex-chains';

const projectId = import.meta.env.VITE_WALLETCONNECT_PROJECT_ID ?? '';

// AppKit hard-rejects an empty projectId at runtime. We surface the
// degraded state to consumers (Connect button disabled) instead of
// crashing the whole app.
export const isWalletConnectAvailable = projectId.length > 0;

// Single-chain tuple matching AppKit's `[Network, ...Network[]]` shape.
// `AppKitNetwork` is a structural superset of viem's `Chain` — our
// stub satisfies it.
const activeStub = getActiveSodexStubChain();
const networks: [AppKitNetwork, ...AppKitNetwork[]] = [activeStub];

// Metadata is shown by the wallet in its connection prompt. Origin
// must match the page origin or some wallets (Rainbow, Trust) reject
// the session request. Icon resolves to the current origin's favicon —
// hardcoding `etfpulse.example.com` would 404 from every wallet's icon
// fetch and surface as a broken image.
const origin = typeof window !== 'undefined' ? window.location.origin : '';
const metadata = {
  name: 'ETFPulse',
  description: 'ETF flow signal intelligence — execute on SoDEX',
  url: origin || 'https://etfpulse.example.com',
  icons: origin ? [`${origin}/favicon.ico`] : [],
};

// Build the wagmi adapter — kept around even when projectId is empty,
// so the WagmiProvider tree has a valid `Config` to mount. The Connect
// button gate prevents users from ever triggering a WalletConnect
// session against the placeholder project id.
const wagmiAdapter = new WagmiAdapter({
  networks,
  projectId: projectId || 'etfpulse-no-walletconnect',
  ssr: false,
});

// Initialise AppKit at module load. The modal is then globally
// available via `useAppKit()`. We only call this when the projectId
// is real — calling it with the placeholder triggers AppKit's
// "invalid project ID" runtime warning every page load.
if (isWalletConnectAvailable) {
  createAppKit({
    adapters: [wagmiAdapter],
    networks,
    projectId,
    metadata,
    defaultNetwork: activeStub,
    features: {
      // ETFPulse is wallet-only auth; strip AppKit's email magic-link,
      // social-login, and analytics-pixel surfaces.
      analytics: false,
      email: false,
      socials: false,
    },
  });
} else {
  // Warn unconditionally (not gated on DEV) so a misconfigured prod
  // deploy surfaces in the browser console — operators investigating
  // a disabled Connect button need this breadcrumb.
  console.warn(
    '[wagmi] VITE_WALLETCONNECT_PROJECT_ID is not set. ' +
      'Wallet connect is disabled. ' +
      'Get a free project ID at https://cloud.reown.com and add it to .env.local (dev) or the deploy env (prod).',
  );
}

export const wagmiConfig = wagmiAdapter.wagmiConfig;
