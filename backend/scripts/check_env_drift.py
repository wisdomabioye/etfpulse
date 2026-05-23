"""Static check: every env-var Settings reads is documented in .env.example.

Task #183. The deployment invariant: `.env.example` is the single source
of truth a new developer (or operator standing up Coolify) consults to
understand what the process needs. If `etfpulse.config.Settings` declares
a field that doesn't appear in `.env.example`, the operator has no way
to know it exists short of reading config.py — drift compounds quickly
(see PR #182 for the 40+ var backfill that prompted this check).

This check is intentionally MINIMAL — it pins one invariant:

    For every field name in Settings.model_fields, EITHER the field's
    canonical UPPERCASE name OR one of its AliasChoices aliases appears
    as a `KEY=` line (commented or uncommented) in .env.example.

It does NOT:
  - Validate the comment quality / explanation.
  - Verify default values in the example match config.py defaults
    (that's a separate, useful check — see task #182's manual reconcile).
  - Catch keys in .env.example that don't map to a Settings field
    (would be too strict — operators sometimes set platform env vars
    for downstream tooling). The diff is reported for visibility but
    does not fail the check.

Exit codes:
    0 — every Settings field is documented.
    1 — at least one field missing, printed to stderr with the canonical
        env-var name and the suggested fix.

Run via `uv run poe env-drift-check`.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Iterable

from pydantic import AliasChoices

from etfpulse.config import Settings

ENV_EXAMPLE_PATH = Path(__file__).resolve().parent.parent / ".env.example"

# Matches `KEY=...` and `# KEY=...` (commented). Anchored at line start so
# prose mentions like "see KEY for details" inside a comment block don't
# falsely count as documentation. `# ?` allows the conventional single
# space between `#` and the key without requiring it.
_KEY_LINE_RE = re.compile(r"^(?:# ?)?([A-Z][A-Z0-9_]+)=", re.MULTILINE)


def collect_settings_keys() -> dict[str, set[str]]:
    """Map each Settings field name → set of accepted env-var names (UPPERCASE).

    The set always contains the field's canonical UPPERCASE name. If the
    field declares `validation_alias=AliasChoices(...)`, each alias is
    added too — accepting EITHER name as valid documentation.

    Pydantic-settings is `case_sensitive=False` so the actual env-var
    lookup is case-blind; the documented convention (and what .env.example
    uses) is UPPERCASE, so the check normalises to that.
    """
    out: dict[str, set[str]] = {}
    for name, field in Settings.model_fields.items():
        accepted = {name.upper()}
        alias = field.validation_alias
        if isinstance(alias, AliasChoices):
            for choice in alias.choices:
                if isinstance(choice, str):
                    accepted.add(choice.upper())
                # AliasPath (nested) isn't used in our config — skip if
                # ever introduced (would need a separate documentation
                # convention anyway).
        out[name] = accepted
    return out


def extract_documented_keys(text: str) -> set[str]:
    """Return the set of env-var names that appear as `KEY=` lines.

    Both `KEY=...` (uncommented — required vars) and `# KEY=...`
    (commented — optional vars with documented defaults) count as
    documentation. Lines that mention a key only in prose
    (e.g. `# see KEY_FOO for details`) do NOT count — they're indexable
    by humans but not actionable for an operator copy-pasting from the file.
    """
    return set(_KEY_LINE_RE.findall(text))


def compute_drift(
    settings_keys: dict[str, set[str]],
    documented: set[str],
) -> tuple[list[str], set[str]]:
    """Return (missing, extra).

    - missing: Settings field names where NONE of the accepted env-var
      names (canonical + aliases) appears in `documented`. Each is a
      hard failure.
    - extra: env-var names in `documented` that don't map to any Settings
      field's accepted set. Reported for visibility; does NOT fail the
      check (operators sometimes document platform vars).
    """
    missing: list[str] = []
    for field_name, accepted in settings_keys.items():
        if not (accepted & documented):
            missing.append(field_name)

    all_accepted: set[str] = set()
    for accepted in settings_keys.values():
        all_accepted |= accepted
    extra = documented - all_accepted

    return sorted(missing), extra


def format_missing_line(field_name: str, accepted: Iterable[str]) -> str:
    """Operator-friendly fix hint for a missing field."""
    names = sorted(accepted)
    if len(names) == 1:
        return f"  {names[0]} — add `# {names[0]}=...` to .env.example (or set as required)"
    return (
        f"  {field_name} — add ANY of [{', '.join(names)}] to .env.example "
        f"(prefer the first / canonical name)"
    )


def main() -> int:
    if not ENV_EXAMPLE_PATH.is_file():
        print(f".env.example not found at {ENV_EXAMPLE_PATH}", file=sys.stderr)
        return 1

    text = ENV_EXAMPLE_PATH.read_text(encoding="utf-8")
    settings_keys = collect_settings_keys()
    documented = extract_documented_keys(text)
    missing, extra = compute_drift(settings_keys, documented)

    if missing:
        print("env-drift check FAILED — task #183:", file=sys.stderr)
        print(
            f"  {len(missing)} Settings field(s) are not documented in .env.example.",
            file=sys.stderr,
        )
        print(file=sys.stderr)
        for name in missing:
            print(format_missing_line(name, settings_keys[name]), file=sys.stderr)
        return 1

    msg = f"OK — all {len(settings_keys)} Settings fields documented in .env.example."
    if extra:
        # Informational: keys in the example that aren't Settings fields.
        # Could be platform vars, intentional, or stale — surface but don't fail.
        msg += f" (info: {len(extra)} extra key(s) in example with no matching Settings field: {sorted(extra)})"
    print(msg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
