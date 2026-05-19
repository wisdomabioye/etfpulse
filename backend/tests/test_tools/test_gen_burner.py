"""Unit tests for `scripts/sodex_verify/gen_burner.py`.

The burner script is operator-only — but its file-handling logic
(refuse-overwrite, --force, --print, chmod 600 enforcement) carries
real safety guarantees. A regression that silently overwrote a funded
burner, or wrote with permissive perms, would be a security smell.
These tests pin the exact behaviour.

We import the script as an in-memory module because `scripts/` isn't
on `sys.path`. Same pattern as `tests/test_tools/test_migration_rollback_check.py`.
"""

from __future__ import annotations

import importlib.util
import json
import stat
import sys
from pathlib import Path

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "sodex_verify" / "gen_burner.py"


def _load_gen_burner():
    """Import the operator script as a module."""
    spec = importlib.util.spec_from_file_location("_gen_burner", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["_gen_burner"] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def gen_burner_module():
    return _load_gen_burner()


@pytest.fixture
def burner_path(tmp_path, monkeypatch):
    """Override the default burner path to a tmp file. We use
    `SODEX_BURNER_PATH` rather than monkeypatching the constant so the
    real env-var resolution path is exercised."""
    path = tmp_path / "burner.json"
    monkeypatch.setenv("SODEX_BURNER_PATH", str(path))
    return path


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


class TestPathResolution:
    def test_env_override_takes_precedence(self, gen_burner_module, tmp_path, monkeypatch):
        override = tmp_path / "custom-burner.json"
        monkeypatch.setenv("SODEX_BURNER_PATH", str(override))
        resolved = gen_burner_module._resolve_burner_path()
        assert resolved == override.resolve()

    def test_default_path_when_no_env(self, gen_burner_module, monkeypatch):
        monkeypatch.delenv("SODEX_BURNER_PATH", raising=False)
        resolved = gen_burner_module._resolve_burner_path()
        # Resolves to the documented default location.
        assert resolved.name == "burner.json"
        assert resolved.parent.name == ".sodex_verify"
        assert str(resolved).startswith(str(Path.home()))

    def test_env_override_expanduser(self, gen_burner_module, monkeypatch):
        monkeypatch.setenv("SODEX_BURNER_PATH", "~/custom/burner.json")
        resolved = gen_burner_module._resolve_burner_path()
        assert "~" not in str(resolved)
        assert str(resolved).startswith(str(Path.home()))


# ---------------------------------------------------------------------------
# Generation + write
# ---------------------------------------------------------------------------


class TestGenerate:
    def test_generates_when_file_absent(self, gen_burner_module, burner_path, capsys):
        rc = gen_burner_module.main([])
        assert rc == 0
        assert burner_path.exists()
        data = json.loads(burner_path.read_text())
        assert data["schema_version"] == 1
        assert data["network"] == "testnet"
        assert data["address"].startswith("0x") and len(data["address"]) == 42
        assert data["private_key"].startswith("0x") and len(data["private_key"]) == 66
        assert "created_at" in data

        # Stdout should contain the address + the export hint.
        captured = capsys.readouterr()
        assert data["address"] in captured.out
        assert "export SODEX_VERIFY_ADDRESS=" in captured.out

    def test_file_is_chmod_600(self, gen_burner_module, burner_path):
        rc = gen_burner_module.main([])
        assert rc == 0
        mode = stat.S_IMODE(burner_path.stat().st_mode)
        assert mode == 0o600, f"expected chmod 600, got {oct(mode)}"

    def test_refuses_overwrite_without_force(self, gen_burner_module, burner_path, capsys):
        # First run creates.
        assert gen_burner_module.main([]) == 0
        original = json.loads(burner_path.read_text())

        # Second run without --force must fail and leave file untouched.
        rc = gen_burner_module.main([])
        assert rc == 1
        assert json.loads(burner_path.read_text()) == original

        captured = capsys.readouterr()
        assert "already exists" in captured.err

    def test_force_overwrites(self, gen_burner_module, burner_path):
        assert gen_burner_module.main([]) == 0
        original = json.loads(burner_path.read_text())

        # --force replaces the key entirely.
        assert gen_burner_module.main(["--force"]) == 0
        replaced = json.loads(burner_path.read_text())
        assert replaced["address"] != original["address"]
        assert replaced["private_key"] != original["private_key"]

    def test_force_preserves_chmod_600(self, gen_burner_module, burner_path):
        assert gen_burner_module.main([]) == 0
        assert gen_burner_module.main(["--force"]) == 0
        mode = stat.S_IMODE(burner_path.stat().st_mode)
        assert mode == 0o600


# ---------------------------------------------------------------------------
# --print mode
# ---------------------------------------------------------------------------


class TestPrintMode:
    def test_print_missing_file_errors(self, gen_burner_module, burner_path, capsys):
        # File doesn't exist yet — --print must NOT generate.
        assert not burner_path.exists()
        rc = gen_burner_module.main(["--print"])
        assert rc == 1
        assert not burner_path.exists()

        captured = capsys.readouterr()
        assert "No burner file" in captured.err

    def test_print_reads_existing(self, gen_burner_module, burner_path, capsys):
        assert gen_burner_module.main([]) == 0
        original = json.loads(burner_path.read_text())
        capsys.readouterr()  # drain prior output

        rc = gen_burner_module.main(["--print"])
        assert rc == 0
        captured = capsys.readouterr()
        assert original["address"] in captured.out
        assert original["private_key"] in captured.out
        # Banner says EXISTING, not NEW.
        assert "EXISTING" in captured.out

    def test_print_does_not_modify_file(self, gen_burner_module, burner_path):
        assert gen_burner_module.main([]) == 0
        before_mtime = burner_path.stat().st_mtime_ns
        before_content = burner_path.read_text()

        assert gen_burner_module.main(["--print"]) == 0
        # File untouched.
        assert burner_path.read_text() == before_content
        assert burner_path.stat().st_mtime_ns == before_mtime


# ---------------------------------------------------------------------------
# Mutually-exclusive flag combination
# ---------------------------------------------------------------------------


class TestFlagCombinations:
    def test_print_and_force_mutually_exclusive(self, gen_burner_module, burner_path, capsys):
        rc = gen_burner_module.main(["--print", "--force"])
        assert rc == 2
        captured = capsys.readouterr()
        assert "mutually exclusive" in captured.err


# ---------------------------------------------------------------------------
# File-format validation
# ---------------------------------------------------------------------------


class TestFileFormatValidation:
    def test_read_rejects_non_dict(self, gen_burner_module, burner_path):
        burner_path.parent.mkdir(parents=True, exist_ok=True)
        burner_path.write_text("[]")
        with pytest.raises(ValueError, match="not a JSON object"):
            gen_burner_module._read_burner(burner_path)

    def test_read_rejects_missing_keys(self, gen_burner_module, burner_path):
        burner_path.parent.mkdir(parents=True, exist_ok=True)
        burner_path.write_text(json.dumps({"address": "0x0"}))
        with pytest.raises(ValueError, match="missing keys"):
            gen_burner_module._read_burner(burner_path)

    def test_read_rejects_schema_mismatch(self, gen_burner_module, burner_path):
        burner_path.parent.mkdir(parents=True, exist_ok=True)
        burner_path.write_text(
            json.dumps(
                {
                    "schema_version": 999,
                    "network": "testnet",
                    "address": "0x0",
                    "private_key": "0x0",
                    "created_at": "2026-01-01T00:00:00+00:00",
                }
            )
        )
        with pytest.raises(ValueError, match="schema mismatch"):
            gen_burner_module._read_burner(burner_path)


# ---------------------------------------------------------------------------
# Atomic write — interrupted writes don't leave the file readable
# ---------------------------------------------------------------------------


class TestAtomicWrite:
    def test_tmp_file_cleaned_up_on_write_error(self, gen_burner_module, burner_path, monkeypatch):
        """If `_write_burner` raises mid-write, the `.tmp` sibling must not
        linger. (We simulate by making `json.dump` raise.)"""
        burner_path.parent.mkdir(parents=True, exist_ok=True)

        original_dump = json.dump

        def raising_dump(*_a, **_kw):
            raise RuntimeError("simulated write failure")

        monkeypatch.setattr(json, "dump", raising_dump)

        with pytest.raises(RuntimeError, match="simulated write failure"):
            gen_burner_module._write_burner(burner_path, gen_burner_module._generate_burner())

        # No `.tmp` files should remain.
        tmp_siblings = list(burner_path.parent.glob("*.tmp"))
        assert not tmp_siblings, f"unexpected tmp files: {tmp_siblings}"

        # And the final path was never written.
        assert not burner_path.exists()

        # Restore for any further tests.
        monkeypatch.setattr(json, "dump", original_dump)
