# ETFPulse frontend

Vite + React 19 + TanStack Query + Tailwind. Pure SPA — talks to the FastAPI backend over `fetch`. No SSR, no API routes, no Next.js.

## Layout

```
src/
  pages/         Home, Signals, SignalDetail, Regime, TrackRecord, Admin
  api/           client.ts (fetch wrapper) + queries.ts (TanStack Query hooks) + types.ts (mirrors backend schemas)
  components/    home/, layout/, signals/, regime/, ui/
  hooks/, lib/   cross-cutting helpers
```

**Anti-drift rule:** backend response shapes (`backend/etfpulse/api/schemas/*.py`) and frontend `src/api/types.ts` must stay in sync — when adding or changing a route, update both in the same change.

## Quick start

```bash
pnpm install
pnpm dev               # http://localhost:5173 — proxies API calls to http://localhost:8000
```

The dev server expects the backend to be running on port 8000. See `../README.md` for the backend quick-start.

## Commands

```bash
pnpm dev               # dev server with HMR
pnpm run lint          # ESLint
pnpm run build         # type-check + production build (outputs to dist/)
pnpm run preview       # serve the production build locally
```

CI runs `pnpm run lint` + `pnpm run build`.

## Build setup

- **React Compiler** is enabled — see [the documentation](https://react.dev/learn/react-compiler). Impacts dev + build performance.
- TypeScript strict mode; no `any` / `unknown` in product code.
- Tailwind + a small `components/ui/` primitive layer — no heavy component framework.

## Deployment

Build output is static — drop `dist/` into any static host (Vercel, Cloudflare Pages, etc). Configure `VITE_API_BASE_URL` to point at the deployed FastAPI backend's public domain.
