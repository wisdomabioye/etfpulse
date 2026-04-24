import { BrowserRouter, Route, Routes } from 'react-router-dom';
import { Container, Footer, TopNav } from './components/layout';

/**
 * App shell — router + global layout.
 *
 * Routes are currently placeholders that verify the layout primitives
 * render. Stage 6d (#75-#77) fills in the real Home / SignalsFeed /
 * SignalDetail pages.
 */
function App() {
  return (
    <BrowserRouter>
      <div className="min-h-screen flex flex-col bg-bg-1 text-text-1">
        <TopNav />
        <main className="flex-1 py-10">
          <Container>
            <Routes>
              <Route path="/" element={<PlaceholderPage title="Home" />} />
              <Route path="/signals" element={<PlaceholderPage title="Signals" />} />
              <Route path="/signals/:id" element={<PlaceholderPage title="Signal detail" />} />
            </Routes>
          </Container>
        </main>
        <Footer />
      </div>
    </BrowserRouter>
  );
}

/** Placeholder component — replaced by real pages in #75-77. */
function PlaceholderPage({ title }: { title: string }) {
  return (
    <div>
      <h1 className="text-2xl font-semibold tracking-tight text-text-1">{title}</h1>
      <p className="mt-2 text-text-3 text-sm">
        Page content lands in Stage 6d. Accent check:{' '}
        <span className="text-accent font-mono">rgb(245, 158, 11)</span>.
      </p>
    </div>
  );
}

export default App;
