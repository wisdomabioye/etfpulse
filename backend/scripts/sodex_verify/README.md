# `scripts/sodex_verify/` — SoDEX verification tooling (operator-only)

**One-time verification scripts used to capture canonical SoDEX
request/response fixtures BEFORE the production execution surface
(Stage 09: C.6 + D.1–D.5) is built. These scripts are NEVER imported by
production backend code.**

## Boundary rule (non-negotiable)

ETFPulse's production backend never holds, generates, signs with, or
stores private keys. SoDEX execution flows go through wallet-side
signing only (wagmi/viem + WalletConnect in the frontend).

This directory is an exception **for verification only**:

- The scripts here generate a throwaway burner key + read it from an
  operator-set env var.
- They run **once** to capture fixtures, which get committed to
  `tests/fixtures/sodex_*.json`.
- After fixtures are committed, the operator destroys the burner key
  (closes the terminal — nothing is persisted).
- **No production code path imports from this directory.** Enforced by
  code review (and a future CI grep if drift becomes a concern).

## Scripts

| Script | Verifies | Used by |
|---|---|---|
| `gen_burner.py` | — | V.0 — generates a fresh testnet burner key, prints to stdout, exits |
| `eip712_capture/` (Go) | EIP-712 typed-data shape | V.1 — produces `tests/fixtures/sodex_eip712_golden.json` (run once locally, commit fixture) |
| `endpoint_probe.py` | REST endpoint URLs + response shapes | V.2 — read-only GETs to spot + perps testnet, records anonymized responses to `tests/fixtures/sodex_endpoint_responses.json`. No signing, no order placement, no funding. |

## Usage flow

```bash
# 1. Generate a burner. Copy the printed env-var lines into your shell.
uv run python scripts/sodex_verify/gen_burner.py
export SODEX_VERIFY_ADDRESS=0x...
export SODEX_VERIFY_PRIVKEY=0x...

# 2. Capture EIP-712 fixtures (one-time, requires Go installed locally).
cd scripts/sodex_verify/eip712_capture && go run . > ../../tests/fixtures/sodex_eip712_golden.json
git add tests/fixtures/sodex_eip712_golden.json && git commit ...

# 3. Probe testnet endpoints (read-only; no signing, no funding needed).
uv run python scripts/sodex_verify/endpoint_probe.py \
    --out tests/fixtures/sodex_endpoint_responses.json

# 4. Destroy the burner: close the terminal, unset SODEX_VERIFY_PRIVKEY.
#    The key was never written to disk.
```

## What these scripts never do

- Never write the private key to a file.
- Never log the private key.
- Never persist anything to the DB.
- Never submit anything to mainnet — `chainId` pinned to SoDEX testnet
  values (`138565` perps, `286623` spot).
- Never get imported by any module under `etfpulse/`.

## Anti-drift

If anyone proposes a code path under `etfpulse/` that imports from
`scripts.sodex_verify`, reject in code review. The boundary is the
whole point.
