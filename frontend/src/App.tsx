import { BrowserRouter, Route, Routes } from 'react-router-dom';
import { AuthProvider } from './auth/AuthContext';
import { Footer, TopNav } from './components/layout';
import { Admin } from './pages/Admin';
import { Analytics } from './pages/Analytics';
import { Home } from './pages/Home';
import { Login } from './pages/Login';
import { Regime } from './pages/Regime';
import { SignalDetail } from './pages/SignalDetail';
import { Signals } from './pages/Signals';
import { TrackRecord } from './pages/TrackRecord';

function App() {
  return (
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
                {/* PR D.4.5 — wallet auth surface. Unlisted from TopNav
                    until D.4.6 adds the /execute page that requires it. */}
                <Route path="/login" element={<Login />} />
                {/* Unlisted from TopNav — operator route, accessed by direct URL. */}
                <Route path="/admin" element={<Admin />} />
              </Routes>
            </main>
            <Footer />
          </div>
        </div>
      </AuthProvider>
    </BrowserRouter>
  );
}

export default App;
