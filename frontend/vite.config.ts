// `defineConfig` imported from `vitest/config` (not `vite`) so the `test`
// block below typechecks. Runtime behaviour is identical to vite's
// defineConfig — vitest re-exports it with the test typings layered on.
import { defineConfig } from 'vitest/config'
import react, { reactCompilerPreset } from '@vitejs/plugin-react'
import babel from '@rolldown/plugin-babel'
import tailwindcss from '@tailwindcss/vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [
    react(),
    babel({ presets: [reactCompilerPreset()] }),
    // Tailwind v4 — picks up `@import "tailwindcss"` in src/index.css and
    // generates only the utility classes used by the source tree.
    tailwindcss(),
  ],
  server: {
    // Dev-only proxy: `fetch('/api/...')` in the SPA hits the FastAPI
    // backend on :8000 without CORS friction. In prod, CORS_ORIGINS on
    // the backend + `VITE_API_BASE_URL` on the frontend handle the split.
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
  // Vitest config (#78.1). Triple-slash ref above imports the `test` field
  // typing into vite's defineConfig. Keeps test config and build config in
  // one file so a setting change can't drift between them.
  test: {
    // jsdom for component tests; happy-dom is faster but our components
    // touch enough DOM surface (CSS computed styles, etc) that jsdom's
    // fuller implementation pays off more than it costs.
    environment: 'jsdom',
    // Auto-import describe/it/expect/etc into every test file. Cuts
    // boilerplate; tradeoff is implicit globals — acceptable for a
    // small frontend test suite.
    globals: true,
    // Setup file runs ONCE per test worker. Extends expect() with
    // jest-dom matchers (toBeInTheDocument, etc).
    setupFiles: ['./src/test/setup.ts'],
    // Don't drag node_modules / dist / build artifacts into the test
    // collection — defaults pick those up.
    include: ['src/**/*.{test,spec}.{ts,tsx}'],
    // Coverage off by default — opt in with `pnpm test -- --coverage`.
    // Run-mode default = watch in TTY, single-run in CI.
  },
})
