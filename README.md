# ETFPulse

ETF-flow-driven signal intelligence for crypto traders. Monitors institutional ETF flows via SoSoValue, detects anomalies, generates AI-powered explanations, and alerts via Telegram.

## Quick Start

```bash
# 1. Copy environment config
cp .env.example .env
# Edit .env with your API keys

# 2. Backend
cd backend
uv sync
uv run uvicorn etfpulse.app:app --reload

# 3. Frontend (separate terminal)
cd frontend
pnpm install
pnpm dev
```

Backend runs on http://localhost:8000, frontend on http://localhost:5173.

## Architecture

See `../build_stages/` for detailed implementation stages and design decisions.

## License

MIT
