"""Static check: every Alembic migration must declare a real downgrade.

Issue #22. The deployment invariant (CLAUDE.md → "Rollback invariant") is
that each migration is backward-compatible with the previous app version
— and that hinges on `downgrade()` actually existing. A `downgrade()`
whose only body is `pass` (or just a docstring, or `return`) means the
migration is one-way; a Coolify rollback to the previous container would
leave the schema ahead of the code.

This check is intentionally MINIMAL — it does not try to assert that
downgrade is the perfect inverse of upgrade (that's what the round-trip
check in `poe migrate-roundtrip` is for, against a live DB). It only
catches the cheap, common mistake: a stub downgrade left behind from
autogen scaffolding.

Exit codes:
    0 — every migration has a non-empty downgrade body.
    1 — at least one violation, printed to stderr with file:line.

Run via `uv run poe migrate-check`.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations" / "versions"


def _is_trivial_body(body: list[ast.stmt]) -> bool:
    """True iff the function body is effectively a no-op.

    A migration whose downgrade is one of:
        def downgrade() -> None: pass
        def downgrade() -> None: ...
        def downgrade() -> None: return
        def downgrade() -> None: '''docstring only'''
    is considered a violation. Everything else (any real op.* call, even
    one) passes — we're not validating correctness, only presence.
    """
    # Strip a leading docstring if present.
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]

    if not body:
        return True

    # Single `pass` / `...` / bare `return` → trivial.
    if len(body) == 1:
        stmt = body[0]
        if isinstance(stmt, ast.Pass):
            return True
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
            # `...` literal
            return stmt.value.value is Ellipsis
        if isinstance(stmt, ast.Return) and stmt.value is None:
            return True

    return False


def _check_file(path: Path) -> list[str]:
    """Return a list of violation messages (empty = file is OK)."""
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:  # pragma: no cover — autogen produces valid Python
        return [f"{path.name}:{exc.lineno}: SyntaxError parsing migration ({exc.msg})"]

    found = False
    violations: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "downgrade":
            found = True
            if _is_trivial_body(node.body):
                violations.append(
                    f"{path.name}:{node.lineno}: downgrade() body is empty / pass / "
                    f"ellipsis. Add real op.* calls or document why a one-way "
                    f"migration is acceptable (issue #22)."
                )
            break
    if not found:
        violations.append(
            f"{path.name}: no `def downgrade()` found. Every migration must declare one."
        )
    return violations


def main() -> int:
    if not MIGRATIONS_DIR.is_dir():
        print(f"migrations dir not found: {MIGRATIONS_DIR}", file=sys.stderr)
        return 1

    files = sorted(p for p in MIGRATIONS_DIR.glob("*.py") if not p.name.startswith("_"))
    if not files:
        # No migrations yet — vacuous pass.
        return 0

    all_violations: list[str] = []
    for path in files:
        all_violations.extend(_check_file(path))

    if all_violations:
        print("Migration rollback check FAILED — issue #22:", file=sys.stderr)
        for msg in all_violations:
            print(f"  {msg}", file=sys.stderr)
        return 1

    print(f"OK — {len(files)} migration(s) declare a non-trivial downgrade.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
