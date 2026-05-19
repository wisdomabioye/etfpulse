"""Generate (or read) a persistent throwaway burner wallet for SoDEX testnet
verification.

Operator-only tool. **NOT production code.** See `scripts/sodex_verify/README.md`
for the full boundary rationale and anti-drift rule.

Why persist (post-V.3 design):
  V.0/V.1/V.2 used the print-and-export model (key lived only in shell
  scrollback). V.3 needs to FUND the burner, REGISTER it as a SoDEX API
  key, then submit real signed writes against it. That lifecycle can't
  happen in a single terminal session, so the burner needs to survive
  across runs.

Storage:
  Default path: `~/.sodex_verify/burner.json` — OUTSIDE the repo, so
  there is no failure mode where a misconfigured `.gitignore` leaks
  the key into git history. The file is `chmod 600` on creation
  (owner read/write only). The directory is `chmod 700`.

  Override via `SODEX_BURNER_PATH` env var if the operator wants a
  different location (e.g. a workspace-mounted secrets directory).
  The override path is treated identically — same chmod + same refuse-
  overwrite semantics.

File format (stable; the Go-based V.3 capture program reads the same
shape):

    {
      "schema_version": 1,
      "network": "testnet",
      "address":     "0x...",
      "private_key": "0x...",
      "created_at":  "2026-05-19T..."
    }

CLI:
  uv run python scripts/sodex_verify/gen_burner.py            # generate (errors if exists)
  uv run python scripts/sodex_verify/gen_burner.py --print    # print existing burner
  uv run python scripts/sodex_verify/gen_burner.py --force    # overwrite (CURRENT KEY IS LOST)

The production backend never imports this module. The boundary is the
whole point of the `scripts/sodex_verify/` carve-out.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path

from eth_account import Account

# File format constants — bumping `_SCHEMA_VERSION` forces consumers
# (this script's `--print`, the V.3 Go capture program) to acknowledge
# the change. Keep it 1 unless the JSON shape actually changes.
_SCHEMA_VERSION = 1

# Default storage location — `~/.sodex_verify/burner.json`. The directory
# matches the credential-file convention (`~/.aws/`, `~/.gnupg/`).
# Operator can override via `SODEX_BURNER_PATH`.
_DEFAULT_BURNER_PATH = Path.home() / ".sodex_verify" / "burner.json"

# File / directory permissions — 600 (owner rw) for the burner file, 700
# (owner rwx) for the parent directory. Anything more permissive would
# let another local user on the same machine read the key. We DO enforce
# these on every write; we do NOT correct existing permissions on read
# (printing the warning is enough — silently chmoding the operator's
# files would be presumptuous).
_FILE_MODE = stat.S_IRUSR | stat.S_IWUSR  # 0o600
_DIR_MODE = stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR  # 0o700


def _resolve_burner_path() -> Path:
    """`SODEX_BURNER_PATH` env override → expanduser; else default."""
    override = os.environ.get("SODEX_BURNER_PATH")
    if override:
        return Path(override).expanduser().resolve()
    return _DEFAULT_BURNER_PATH


def _print_banner(stream, title: str) -> None:
    bar = "-" * 70
    stream.write(f"{bar}\n{title}\n{bar}\n\n")


def _print_warnings(stream, path: Path) -> None:
    bar = "-" * 70
    stream.write("\n")
    stream.write("WARNINGS (read carefully):\n")
    stream.write("  - For SoDEX testnet verification (V.1, V.2, V.3) ONLY.\n")
    stream.write("  - NEVER commit this file. NEVER share. NEVER reuse on mainnet.\n")
    stream.write("  - File mode: chmod 600 (only you can read).\n")
    stream.write(f"  - Path: {path}\n")
    stream.write("  - When all fixtures are committed and stable, delete the file:\n")
    stream.write(f"      rm {path}\n")
    stream.write(f"{bar}\n")


def _generate_burner() -> dict:
    """Generate a fresh burner key. Uses `eth_account.Account.create()`
    which seeds from `os.urandom` — cryptographically secure.
    """
    acct = Account.create()
    raw_hex = acct.key.hex()
    privkey = raw_hex if raw_hex.startswith("0x") else f"0x{raw_hex}"
    return {
        "schema_version": _SCHEMA_VERSION,
        "network": "testnet",
        "address": acct.address,  # EIP-55 checksummed
        "private_key": privkey,
        "created_at": datetime.now(UTC).isoformat(),
    }


def _write_burner(path: Path, burner: dict) -> None:
    """Write burner JSON with strict permissions. Creates parent dir if needed.

    Atomic write: write to a tmp file in the same directory, chmod 600,
    then rename. Prevents a half-written file being readable mid-write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # Tighten directory perms (best-effort; existing dir won't change owner).
    try:
        os.chmod(path.parent, _DIR_MODE)
    except OSError:
        pass  # Non-fatal — the file's own 0o600 is the real protection.

    tmp_path = path.with_suffix(path.suffix + ".tmp")
    # `os.open` with `O_CREAT | O_EXCL | O_WRONLY` + mode bits creates
    # the file with strict perms atomically (no `chmod` race between
    # `open` and `chmod`).
    fd = os.open(str(tmp_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, _FILE_MODE)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(burner, fh, indent=2, sort_keys=False)
            fh.write("\n")
    except Exception:
        # Clean up the tmp file if write failed.
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        raise
    # Rename is atomic on POSIX. Old file (if any) is replaced.
    os.replace(tmp_path, path)


def _read_burner(path: Path) -> dict:
    """Read + validate burner JSON. Raises ValueError on shape drift."""
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"Burner file at {path} is not a JSON object")
    expected_keys = {"schema_version", "network", "address", "private_key", "created_at"}
    missing = expected_keys - set(data)
    if missing:
        raise ValueError(f"Burner file at {path} missing keys: {sorted(missing)}")
    if data["schema_version"] != _SCHEMA_VERSION:
        raise ValueError(
            f"Burner file schema mismatch at {path}: got {data['schema_version']!r}, "
            f"expected {_SCHEMA_VERSION}. Regenerate with --force."
        )
    return data


def _print_burner(stream, burner: dict, path: Path, *, freshly_generated: bool) -> None:
    title = "SODEX TESTNET BURNER (NEW)" if freshly_generated else "SODEX TESTNET BURNER (EXISTING)"
    _print_banner(stream, title)
    stream.write(f"Address:      {burner['address']}\n")
    stream.write(f"Private key:  {burner['private_key']}\n")
    stream.write(f"Network:      {burner['network']}\n")
    stream.write(f"Created at:   {burner['created_at']}\n")
    stream.write("\nFor shells that need env vars (V.1 Go capture, manual flows):\n\n")
    stream.write(f"  export SODEX_VERIFY_ADDRESS={burner['address']}\n")
    stream.write(f"  export SODEX_VERIFY_PRIVKEY={burner['private_key']}\n")
    _print_warnings(stream, path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate or read the SoDEX testnet burner wallet."
    )
    parser.add_argument(
        "--print",
        dest="print_only",
        action="store_true",
        help="Print existing burner contents; do not generate.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite the existing burner. CURRENT KEY IS LOST (and any funds on it).",
    )
    args = parser.parse_args(argv)

    if args.print_only and args.force:
        sys.stderr.write("ERROR: --print and --force are mutually exclusive.\n")
        return 2

    path = _resolve_burner_path()

    if args.print_only:
        if not path.exists():
            sys.stderr.write(f"ERROR: No burner file at {path}.\n")
            sys.stderr.write("Run `gen_burner.py` (no flag) to generate one.\n")
            return 1
        burner = _read_burner(path)
        _print_burner(sys.stdout, burner, path, freshly_generated=False)
        return 0

    if path.exists() and not args.force:
        sys.stderr.write(f"ERROR: Burner already exists at {path}.\n")
        sys.stderr.write(
            "Use `--print` to read it, or `--force` to overwrite (CURRENT KEY IS LOST).\n"
        )
        return 1

    burner = _generate_burner()
    _write_burner(path, burner)
    _print_burner(sys.stdout, burner, path, freshly_generated=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
