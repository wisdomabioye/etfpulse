import { BrowserRouter, Route, Routes } from 'react-router-dom';
import { Footer, TopNav } from './components/layout';
import { Home } from './pages/Home';
import { SignalDetail } from './pages/SignalDetail';
import { Signals } from './pages/Signals';

function App() {
  return (
    <BrowserRouter>
      <div className="min-h-screen bg-bg-0 p-4 sm:p-6">
        <div className="max-w-7xl mx-auto bg-bg-1 border border-border-2 rounded-xl overflow-hidden flex flex-col min-h-[calc(100vh-2rem)] sm:min-h-[calc(100vh-3rem)] text-text-1">
          <TopNav />
          <main className="flex-1">
            <Routes>
              <Route path="/" element={<Home />} />
              <Route path="/signals" element={<Signals />} />
              <Route path="/signals/:id" element={<SignalDetail />} />
            </Routes>
          </main>
          <Footer />
        </div>
      </div>
    </BrowserRouter>
  );
}

export default App;
