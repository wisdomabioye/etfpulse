"""Unit tests for `scripts/check_migration_rollback.py`.

The checker is a guardrail — if its detection logic regresses (false
negatives on stub downgrades, or false positives on real ones), broken
migrations land in CI silently. These tests pin the exact behaviour.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "check_migration_rollback.py"


def _load_checker():
    """Import the script as a module — it's not on `sys.path` by default."""
    spec = importlib.util.spec_from_file_location("_rollback_check", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["_rollback_check"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def checker():
    return _load_checker()


# ---------------------------------------------------------------------------
# Stub downgrades — should be flagged.
# ---------------------------------------------------------------------------


def _write_migration(tmp_path: Path, name: str, body: str) -> Path:
    """Build a migration file with the given downgrade() body."""
    src = f'''"""test migration"""

def upgrade() -> None:
    pass


def downgrade() -> None:
{body}
'''
    path = tmp_path / f"{name}.py"
    path.write_text(src, encoding="utf-8")
    return path


class TestTrivialBodies:
    """Each of these should produce one violation."""

    def test_pass_only(self, checker, tmp_path):
        path = _write_migration(tmp_path, "p1", "    pass")
        assert len(checker._check_file(path)) == 1

    def test_ellipsis_only(self, checker, tmp_path):
        path = _write_migration(tmp_path, "p2", "    ...")
        assert len(checker._check_file(path)) == 1

    def test_bare_return(self, checker, tmp_path):
        path = _write_migration(tmp_path, "p3", "    return")
        assert len(checker._check_file(path)) == 1

    def test_docstring_only(self, checker, tmp_path):
        path = _write_migration(tmp_path, "p4", '    """nothing yet"""')
        assert len(checker._check_file(path)) == 1

    def test_docstring_plus_pass(self, checker, tmp_path):
        path = _write_migration(tmp_path, "p5", '    """nothing"""\n    pass')
        assert len(checker._check_file(path)) == 1


class TestRealBodies:
    """Each of these has actual work — should pass."""

    def test_single_op_call(self, checker, tmp_path):
        path = _write_migration(tmp_path, "ok1", '    op.drop_column("x", "y")')
        assert checker._check_file(path) == []

    def test_docstring_plus_op_call(self, checker, tmp_path):
        path = _write_migration(tmp_path, "ok2", '    """why"""\n    op.drop_table("t")')
        assert checker._check_file(path) == []


class TestMissingDowngrade:
    def test_no_downgrade_function(self, checker, tmp_path):
        src = '''"""no downgrade defined"""

def upgrade() -> None:
    op.add_column("x", "y")
'''
        path = tmp_path / "missing.py"
        path.write_text(src, encoding="utf-8")
        violations = checker._check_file(path)
        assert len(violations) == 1
        assert "no `def downgrade()`" in violations[0]


class TestRealMigrations:
    """Sanity check the live migrations on disk still pass — guards against
    a future check tightening that would silently invalidate them."""

    def test_existing_migrations_all_pass(self, checker):
        for path in checker.MIGRATIONS_DIR.glob("*.py"):
            if path.name.startswith("_"):
                continue
            assert checker._check_file(path) == [], (
                f"existing migration {path.name} flagged by the checker — "
                f"either fix the migration or relax the checker"
            )
