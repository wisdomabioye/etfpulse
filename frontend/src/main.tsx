import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { WagmiProvider } from 'wagmi'
import './index.css'
import App from './App.tsx'
import { wagmiConfig } from './lib/wagmi'

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

// Provider order: WagmiProvider OUTSIDE QueryClientProvider because wagmi
// v2 piggybacks on the same `@tanstack/react-query` instance — wagmi's
// hooks need to find the QueryClient in context. Wrapping wagmi outside
// then query inside keeps wagmi's mutations addressable from any descendant.
createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <WagmiProvider config={wagmiConfig}>
      <QueryClientProvider client={queryClient}>
        <App />
      </QueryClientProvider>
    </WagmiProvider>
  </StrictMode>,
)
