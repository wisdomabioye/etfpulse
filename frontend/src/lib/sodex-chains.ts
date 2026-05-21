/**
 * SoDEX EIP-712 signing-domain stub.
 *
 * Honesty disclaimer: SoDEX is NOT a chain. It's an off-chain
 * order-routing gateway that verifies EIP-712 signed payloads. There
 * is no SoDEX RPC node, no SoDEX block explorer, no native SoDEX
 * token. The "chainId" (138565 testnet / 286623 mainnet) is purely a
 * domain-separator constant in the EIP-712 message — same conceptual
 * role as a magic number, not a real EVM chain you can connect to.
 *
 * Why we still register a "chain" with wagmi/AppKit:
 *   wagmi v2's TypeScript config + Reown AppKit's networks array both
 *   require at least one `Chain` object. We pay this overhead because
 *   the alternative (no wallet-connect UI) loses MetaMask / Rainbow /
 *   Coinbase Wallet support. The fields below are wagmi-required
 *   PLACEHOLDERS — wallets will display them in their network-switch
 *   prompt, but the prompt itself is meaningless for SoDEX (the user
 *   doesn't actually need to "switch" anywhere; `signTypedData` works
 *   regardless of the wallet's connected chain).
 *
 * What's real here:
 *   - `id` — V.3-verified against the live SoDEX testnet (138565),
 *     and per api.md for mainnet (286623). Backend pins the same
 *     values in `settings.sodex_{testnet,mainnet}_chain_id`.
 *
 * What's placeholder (wagmi-required, semantically meaningless):
 *   - `name` — display string in wallet UIs.
 *   - `nativeCurrency` — wallets show this when prompting to switch.
 *     SoDEX has no native gas token; we use 18 decimals to match
 *     EVM-tooling defaults so wallets don't render strange numbers.
 *   - `rpcUrls` — viem requires a non-empty URL. We use `.invalid` so
 *     a regression that accidentally calls JSON-RPC fails fast at DNS.
 *
 * Anti-drift contract: the FE's `chainId` MUST match the backend's
 * `settings.sodex_chain_id` exactly, or `/api/wallet/verify` rejects
 * every SIWE with `chain_id mismatch`. The cleanest pin is
 * `getActiveSodexChainId()` — use this single function everywhere
 * EIP-712 typed-data is built, never hardcode the integer.
 */

import { defineChain } from 'viem';

// Verified against backend + V.3 captures. These are the ONLY two
// real things in this file.
export const SODEX_TESTNET_CHAIN_ID = 138565;
export const SODEX_MAINNET_CHAIN_ID = 286623;

// Placeholder RPC — never called at runtime. `.invalid` is a reserved
// TLD per RFC 2606; DNS resolution fails immediately if a future
// regression introduces a `getBlockNumber` call.
const RPC_PLACEHOLDER = 'https://sodex-rpc-placeholder.invalid';

// Wagmi-required placeholder native currency. NOT a real SoDEX token.
// The wallet's network-switch prompt will display these strings; users
// shouldn't expect them to mean anything (SoDEX doesn't HAVE a native
// gas token — the gateway is off-chain). 18 decimals matches EVM
// tooling defaults so wallets don't render strange precision.
const PLACEHOLDER_NATIVE_CURRENCY = {
  name: 'SoDEX (off-chain)',
  symbol: 'SDX',
  decimals: 18,
} as const;

const sodexSigningStubTestnet = defineChain({
  id: SODEX_TESTNET_CHAIN_ID,
  name: 'SoDEX Testnet (signing only)',
  nativeCurrency: PLACEHOLDER_NATIVE_CURRENCY,
  rpcUrls: { default: { http: [RPC_PLACEHOLDER] } },
  testnet: true,
});

const sodexSigningStubMainnet = defineChain({
  id: SODEX_MAINNET_CHAIN_ID,
  name: 'SoDEX (signing only)',
  nativeCurrency: PLACEHOLDER_NATIVE_CURRENCY,
  rpcUrls: { default: { http: [RPC_PLACEHOLDER] } },
});

/**
 * Resolve the active signing-domain chainId at app boot.
 *
 * Pinned from `VITE_SODEX_CHAIN_ID`; defaults to testnet so a
 * misconfigured dev env can't accidentally sign payloads for mainnet.
 * Use this everywhere EIP-712 typed-data is built — hardcoding the
 * integer makes a future testnet→mainnet flip a multi-file change.
 */
export function getActiveSodexChainId(): number {
  const raw = import.meta.env.VITE_SODEX_CHAIN_ID;
  if (!raw) return SODEX_TESTNET_CHAIN_ID;
  const id = Number(raw);
  if (id === SODEX_MAINNET_CHAIN_ID || id === SODEX_TESTNET_CHAIN_ID) {
    return id;
  }
  // Typo / unknown value. Backend would reject every SIWE verify with
  // `chain_id mismatch` and the user has no FE-side hint of the
  // misconfig. Warn on first paint so the operator notices.
  console.warn(
    `[sodex-chains] VITE_SODEX_CHAIN_ID=${raw} is not a known SoDEX ` +
      `signing domain (testnet=${SODEX_TESTNET_CHAIN_ID}, ` +
      `mainnet=${SODEX_MAINNET_CHAIN_ID}). Falling back to testnet. ` +
      `Backend SODEX_ENVIRONMENT must align with this value.`,
  );
  return SODEX_TESTNET_CHAIN_ID;
}

/**
 * Resolve the active stub chain for wagmi/AppKit registration.
 *
 * Returns ONE chain (not both) — the user never "switches" between
 * SoDEX testnet and mainnet at runtime; the active env is fixed at
 * deploy time. Registering both would invite confusing network-switch
 * UI for a switch that doesn't make sense.
 */
export function getActiveSodexStubChain() {
  const id = getActiveSodexChainId();
  return id === SODEX_MAINNET_CHAIN_ID ? sodexSigningStubMainnet : sodexSigningStubTestnet;
}
