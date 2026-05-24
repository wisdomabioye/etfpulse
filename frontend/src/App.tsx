import { Suspense, lazy } from 'react';
import { BrowserRouter, Route, Routes } from 'react-router-dom';
import { AuthProvider } from './auth/AuthContext';
import { ErrorBoundary } from './components/ErrorBoundary';
import { Footer, TopNav } from './components/layout';
import { Analytics } from './pages/Analytics';
import { Home } from './pages/Home';
import { Regime } from './pages/Regime';
import { SignalDetail } from './pages/SignalDetail';
import { Signals } from './pages/Signals';
import { TrackRecord } from './pages/TrackRecord';

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

/**
 * Suspense fallback while the wallet chunk loads. Kept identical-width to
 * the surrounding layout so the page doesn't reflow when the chunk lands.
 * The text is intentionally generic — users see this for fractions of a
 * second on a warm browser cache, longer on a cold load. No spinner: at
 * sub-second loads a spinner appears + disappears jarringly.
 */
function WalletRouteFallback() {
  return (
    <div className="min-h-[60vh] flex items-center justify-center text-text-3 text-sm">
      Loading wallet…
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
          <div className="min-h-screen bg-bg-0 p-4 sm:p-6">
            <div className="max-w-7xl mx-auto bg-bg-1 border border-border-2 rounded-xl overflow-hidden flex flex-col min-h-[calc(100vh-2rem)] sm:min-h-[calc(100vh-3rem)] text-text-1">
              <TopNav />
              <main className="flex-1">
                <Routes>
                  <Route path="/" element={<Home />} />
                  <Route path="/signals" element={<Signals />} />
                  <Route path="/signals/:id" element={<SignalDetail />} />
                  <Route path="/regime" element={<Regime />} />
                  <Route path="/track-record" element={<TrackRecord />} />
                  <Route path="/analytics" element={<Analytics />} />
                  {/* PR D.4.5/D.4.6 — wallet auth + execute surfaces.
                      Reachable via the "Trade" nav entry (#181) and from
                      the bot's `/execute` WebApp button. Lazy-loaded
                      behind WalletProviders so the wagmi bundle stays
                      out of the public-page payload (#78.5). */}
                  <Route
                    path="/login"
                    element={
                      <Suspense fallback={<WalletRouteFallback />}>
                        <WalletProviders>
                          <Login />
                        </WalletProviders>
                      </Suspense>
                    }
                  />
                  <Route
                    path="/execute"
                    element={
                      <Suspense fallback={<WalletRouteFallback />}>
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
                      <Suspense fallback={<WalletRouteFallback />}>
                        <Admin />
                      </Suspense>
                    }
                  />
                </Routes>
              </main>
              <Footer />
            </div>
          </div>
        </AuthProvider>
      </BrowserRouter>
    </ErrorBoundary>
  );
}

export default App;
