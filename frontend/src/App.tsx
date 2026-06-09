import { Suspense, lazy } from 'react';
import { BrowserRouter, Route, Routes } from 'react-router-dom';
import { AuthProvider } from './auth/AuthContext';
import { ErrorBoundary } from './components/ErrorBoundary';
import { Footer, MobileTabBar, TopNav } from './components/layout';
import { Analytics } from './pages/Analytics';
import { Home } from './pages/Home';
import { Methodology } from './pages/Methodology';
import { Proof } from './pages/Proof';
import { Regime } from './pages/Regime';
import { SignalDetail } from './pages/SignalDetail';
import { Signals } from './pages/Signals';

// #78.5 — Lazy-load the wallet-using pages + the WagmiProvider wrapper.
// Vite splits these into their own chunks; the 1.5 MB AppKit + wagmi +
// viem bundle only loads when the user navigates to /login or /execute.
// Public pages (Home / Signals / Regime / TrackRecord / Analytics) cost
// nothing in initial bundle.
//
// `.then(m => ({ default: m.X }))` shim because Login/Execute are named
// exports; React.lazy requires a default export. WalletProviders is
// default-exported and doesn't need the shim.
const WalletProviders = lazy(() => import('./lib/WalletProviders'));
const Login = lazy(() => import('./pages/Login').then((m) => ({ default: m.Login })));
const Execute = lazy(() => import('./pages/Execute').then((m) => ({ default: m.Execute })));
// Admin is unlisted from TopNav (operator-only, accessed by direct URL).
// Lazy so its ~30 kB doesn't bloat the public-page bundle (#186).
const Admin = lazy(() => import('./pages/Admin').then((m) => ({ default: m.Admin })));
// Admin/backtest also lazy — same operator-only rationale.
const BacktestPage = lazy(() =>
  import('./pages/admin/Backtest').then((m) => ({ default: m.BacktestPage })),
);

/**
 * Generic suspense fallback for lazy-loaded routes. `label` says what
 * the user is waiting for so `/admin/backtest` doesn't claim "wallet"
 * and `/login` doesn't claim "admin". Identical-width layout to the
 * surrounding page shell so nothing reflows when the chunk lands.
 * The text is intentionally minimal — users see this for fractions of
 * a second on a warm cache, longer on a cold load. No spinner: at
 * sub-second loads a spinner appears + disappears jarringly.
 */
function LazyRouteFallback({ label }: { label: string }) {
  return (
    <div className="min-h-[60vh] flex items-center justify-center text-t3 text-sm">
      Loading {label}…
    </div>
  );
}

function App() {
  return (
    <ErrorBoundary>
      <BrowserRouter>
        {/* AuthProvider lives INSIDE BrowserRouter — its 401 interceptor
            needs `useNavigate()` from the router context. */}
        <AuthProvider>
          {/* Full-bleed shell (matches the prototype): bg-0 background, sticky
              TopNav, centered max-width Page bodies. The old framed rounded
              card defeated position:sticky — removed so nav + filter bars
              actually stick. */}
          <div className="min-h-screen bg-bg-0 text-t1 flex flex-col pb-16 md:pb-0">
              <TopNav />
              <main className="flex-1">
                <Routes>
                  <Route path="/" element={<Home />} />
                  <Route path="/signals" element={<Signals />} />
                  <Route path="/signals/:id" element={<SignalDetail />} />
                  <Route path="/regime" element={<Regime />} />
                  <Route path="/track-record" element={<Proof />} />
                  <Route path="/analytics" element={<Analytics />} />
                  <Route path="/methodology" element={<Methodology />} />
                  {/* PR D.4.5/D.4.6 — wallet auth + execute surfaces.
                      Reachable via the "Trade" nav entry (#181) and from
                      the bot's `/execute` WebApp button. Lazy-loaded
                      behind WalletProviders so the wagmi bundle stays
                      out of the public-page payload (#78.5). */}
                  <Route
                    path="/login"
                    element={
                      <Suspense fallback={<LazyRouteFallback label="wallet" />}>
                        <WalletProviders>
                          <Login />
                        </WalletProviders>
                      </Suspense>
                    }
                  />
                  <Route
                    path="/execute"
                    element={
                      <Suspense fallback={<LazyRouteFallback label="wallet" />}>
                        <WalletProviders>
                          <Execute />
                        </WalletProviders>
                      </Suspense>
                    }
                  />
                  {/* Unlisted from TopNav — operator route, accessed by direct URL.
                      Lazy-loaded (#186) so the operator surface doesn't bloat
                      the public-page bundle for the 99%+ of visitors who'll
                      never hit it. */}
                  <Route
                    path="/admin"
                    element={
                      <Suspense fallback={<LazyRouteFallback label="admin" />}>
                        <Admin />
                      </Suspense>
                    }
                  />
                  <Route
                    path="/admin/backtest"
                    element={
                      <Suspense fallback={<LazyRouteFallback label="admin" />}>
                        <BacktestPage />
                      </Suspense>
                    }
                  />
                </Routes>
              </main>
              <Footer />
              {/* Mobile bottom tab bar — fixed to the viewport; hidden ≥md
                  where the TopNav row carries nav. */}
              <MobileTabBar />
          </div>
        </AuthProvider>
      </BrowserRouter>
    </ErrorBoundary>
  );
}

export default App;
