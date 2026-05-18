"""Generate a throwaway burner wallet for SoDEX testnet verification.

Operator-only tool. **NOT production code.** See `scripts/sodex_verify/README.md`
for the full boundary rationale.

Prints address + private key to stdout, exits. Nothing is written to disk,
logged, or persisted in any way. Operator copies the printed `export` lines
into the shell session that will run V.1 (`eip712_capture/`) and V.2
(`endpoint_probe.py`), then destroys the key by closing the terminal.

This script is the ONLY place in the repo that generates a private key.
The production backend has no path to `eth_account.Account.create()` —
that's the architectural invariant. After V.0/V.1/V.2 fixtures land, this
script's continued existence is for future re-captures only (e.g., if SoDEX
bumps the EIP-712 spec). It MUST NOT be imported.

Run from `backend/`:

    uv run python scripts/sodex_verify/gen_burner.py
"""

from __future__ import annotations

import sys

from eth_account import Account


def _print_header(stream) -> None:
    bar = "-" * 70
    stream.write(f"{bar}\n")
    stream.write("SODEX TESTNET BURNER WALLET\n")
    stream.write(f"{bar}\n\n")


def _print_warnings(stream) -> None:
    bar = "-" * 70
    stream.write("\n")
    stream.write("WARNINGS (read carefully):\n")
    stream.write("  - For SoDEX testnet verification scripts ONLY (V.1 + V.2).\n")
    stream.write("  - NEVER commit this key. NEVER share it. NEVER reuse on mainnet.\n")
    stream.write("  - The key is in your terminal scrollback. Clear it after use:\n")
    stream.write("      history -c   (bash/zsh)\n")
    stream.write("  - When V.1/V.2 fixtures are committed, unset the env var and\n")
    stream.write("    close the terminal. The key was never written to disk.\n")
    stream.write(f"{bar}\n")


def main() -> int:
    # Use `eth_account` directly. `Account.create()` reads from os.urandom
    # — cryptographically secure. We do NOT pass `extra_entropy`; the OS
    # entropy is sufficient for a single-session throwaway key.
    acct = Account.create()
    address = acct.address  # checksummed EIP-55
    # `HexBytes.hex()` returns the hex WITHOUT the 0x prefix in current
    # eth_account; the rest of the SoDEX-side tooling expects 0x-prefixed
    # values everywhere. Normalise explicitly so operator-pasted env vars
    # are immediately usable.
    raw_hex = acct.key.hex()
    privkey = raw_hex if raw_hex.startswith("0x") else f"0x{raw_hex}"

    out = sys.stdout
    _print_header(out)
    out.write(f"Address:      {address}\n")
    out.write(f"Private key:  {privkey}\n")
    out.write("\nFor the next steps, run these in your shell:\n\n")
    out.write(f"  export SODEX_VERIFY_ADDRESS={address}\n")
    out.write(f"  export SODEX_VERIFY_PRIVKEY={privkey}\n")
    _print_warnings(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
