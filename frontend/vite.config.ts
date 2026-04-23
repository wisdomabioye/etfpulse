import { defineConfig } from 'vite'
import react, { reactCompilerPreset } from '@vitejs/plugin-react'
import babel from '@rolldown/plugin-babel'

// https://vite.dev/config/
export default defineConfig({
  plugins: [
    react(),
    babel({ presets: [reactCompilerPreset()] })
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
})
