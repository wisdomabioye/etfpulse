import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import './index.css'
import App from './App.tsx'

// Single QueryClient for the app. Defaults:
//   - staleTime 30s — dashboard stats + feed don't need sub-second freshness;
//     30s matches the backend's delivery-worker tick, so post-fan-out data
//     lands on the next natural refetch.
//   - retry 1 — fail fast on 4xx/5xx; the user sees the error instead of a
//     spinner for 3+ seconds of retries.
//   - refetchOnWindowFocus false — app is long-running in a tab; constant
//     refetch on tab switches is churn, not "freshness."
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      retry: 1,
      refetchOnWindowFocus: false,
    },
  },
})

// #78.5 — WagmiProvider USED to be mounted here at the root, which
// pulled the 1.5 MB AppKit + wagmi + viem bundle onto every page,
// including public pages (Home / Signals / TrackRecord / Regime /
// Analytics) that don't use wallet hooks. It now lives in
// `lib/WalletProviders.tsx`, lazy-imported only on `/login` +
// `/execute` (see App.tsx). Public pages no longer pay the cost.
//
// QueryClient stays at the root — wagmi v2 piggybacks on the same
// `@tanstack/react-query` instance, but it doesn't matter that
// QueryClient is mounted ABOVE WagmiProvider in the tree (wagmi's
// hooks look up QueryClient via context, which is available in any
// descendant).
createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>
  </StrictMode>,
)
