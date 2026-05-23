"""Unit tests for `scripts/check_env_drift.py`.

The checker is a guardrail — if its detection logic regresses, undocumented
Settings fields ship silently and the next operator standing up Coolify
discovers them by trial and error. These tests pin:

  1. The end-to-end contract against the real `.env.example` (zero missing).
  2. The pure-helper behaviour (regex correctness, alias acceptance, prose-
     mention exclusion, case-insensitive normalisation).

Style mirrors `test_migration_rollback_check.py` — load the script as a
module via `importlib`, then exercise its pure functions directly so the
helpers stay testable without subprocess overhead.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "check_env_drift.py"
_ENV_EXAMPLE_PATH = Path(__file__).resolve().parents[2] / ".env.example"


def _load_checker():
    """Import the script as a module — it's not on `sys.path` by default."""
    spec = importlib.util.spec_from_file_location("_env_drift_check", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["_env_drift_check"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def checker():
    return _load_checker()


# ---------------------------------------------------------------------------
# End-to-end — the live invariant. Catches the real regression.
# ---------------------------------------------------------------------------


def test_live_settings_match_live_env_example(checker):
    """Against the real Settings + real .env.example, there must be zero
    missing fields. This is the test that fails CI when someone adds a
    new Settings field and forgets to document it."""
    text = _ENV_EXAMPLE_PATH.read_text(encoding="utf-8")
    settings_keys = checker.collect_settings_keys()
    documented = checker.extract_documented_keys(text)
    missing, _extra = checker.compute_drift(settings_keys, documented)
    assert missing == [], (
        f"{len(missing)} Settings field(s) not documented in .env.example: {missing}. "
        f"Add each as `# KEY=default` (or as a required var) and re-run."
    )


def test_main_returns_zero_against_live_files(checker):
    """`scripts/check_env_drift.py` exits 0 when there's no drift.
    Pins the CLI contract (poe wires this command into the check pipeline)."""
    assert checker.main() == 0


# ---------------------------------------------------------------------------
# extract_documented_keys — regex correctness
# ---------------------------------------------------------------------------


def test_extract_uncommented_required_var(checker):
    """Required vars are written as `KEY=...` (no leading #) — must count."""
    text = "DATABASE_URL=postgresql://localhost/foo\n"
    assert checker.extract_documented_keys(text) == {"DATABASE_URL"}


def test_extract_commented_optional_var(checker):
    """Optional vars are written as `# KEY=default` — must count too."""
    text = "# CALIBRATION_LOOKBACK_DAYS=90\n"
    assert checker.extract_documented_keys(text) == {"CALIBRATION_LOOKBACK_DAYS"}


def test_extract_commented_no_space_after_hash(checker):
    """`#KEY=...` (no space) is unusual but should still count — operators
    write `#` directly in some editors and the docstring `# ?` regex
    explicitly admits this form."""
    text = "#FOO_BAR=1\n"
    assert checker.extract_documented_keys(text) == {"FOO_BAR"}


def test_prose_mention_does_not_count(checker):
    """A KEY mentioned in flowing prose (no `=` immediately after the name)
    is NOT documentation — operators can't copy-paste it. The regex's
    `=` anchor enforces this."""
    text = "# See ACCELERATION_MIN_PRIOR_USD in the deprecation notes.\n"
    assert checker.extract_documented_keys(text) == set()


def test_indented_key_line_does_not_count(checker):
    """A line that's not anchored at column 0 (e.g. inside a code block
    or accidentally indented) is malformed for an env file — must not
    falsely count as documented."""
    text = "  KEY_INDENTED=value\n"
    # The regex anchors with `^` which under MULTILINE matches start-of-line
    # — so `  KEY_INDENTED=` starts with spaces, not the regex's allowed
    # `(# ?)?` prefix. Verified excluded.
    assert checker.extract_documented_keys(text) == set()


def test_lowercase_keys_do_not_match(checker):
    """Env-var convention is UPPERCASE. A lowercase line (`foo_bar=`) is
    not standards-compliant and is excluded by the `[A-Z][A-Z0-9_]+`
    character class — operators reading the file expect uppercase."""
    text = "foo_bar=value\n"
    assert checker.extract_documented_keys(text) == set()


def test_multiple_keys_across_lines(checker):
    text = """
DATABASE_URL=postgres://localhost/foo
# CORS_ORIGINS=http://localhost:5173
# OPENROUTER_DAILY_CALL_CAP=100
"""
    assert checker.extract_documented_keys(text) == {
        "DATABASE_URL",
        "CORS_ORIGINS",
        "OPENROUTER_DAILY_CALL_CAP",
    }


# ---------------------------------------------------------------------------
# collect_settings_keys — alias handling
# ---------------------------------------------------------------------------


def test_collect_includes_canonical_name_uppercased(checker):
    """Every field MUST have at least its UPPERCASE canonical name in the
    accepted set — that's the no-alias baseline."""
    settings_keys = checker.collect_settings_keys()
    assert "DATABASE_URL" in settings_keys["database_url"]
    assert "JWT_SECRET" in settings_keys["jwt_secret"]


def test_collect_includes_validation_aliases(checker):
    """The one AliasChoices field in config.py is
    `acceleration_min_slope_old_usd`, which accepts the legacy
    `acceleration_min_prior_usd` name. The check MUST accept either as
    documentation — otherwise the deprecation path would force operators
    to migrate the env var name AND update .env.example in lockstep,
    defeating the AliasChoices."""
    settings_keys = checker.collect_settings_keys()
    accepted = settings_keys["acceleration_min_slope_old_usd"]
    assert "ACCELERATION_MIN_SLOPE_OLD_USD" in accepted
    assert "ACCELERATION_MIN_PRIOR_USD" in accepted


# ---------------------------------------------------------------------------
# compute_drift — semantics
# ---------------------------------------------------------------------------


def test_compute_drift_missing_when_no_accepted_name_present(checker):
    """A field is missing if NONE of its accepted env-var names appear
    in the documented set."""
    settings_keys = {"foo": {"FOO"}}
    documented: set[str] = set()
    missing, extra = checker.compute_drift(settings_keys, documented)
    assert missing == ["foo"]
    assert extra == set()


def test_compute_drift_alias_satisfies_canonical(checker):
    """If only the alias appears in .env.example, the field is still
    documented — the operator's env var works through AliasChoices."""
    settings_keys = {"foo": {"FOO_NEW", "FOO_OLD"}}
    documented = {"FOO_OLD"}
    missing, extra = checker.compute_drift(settings_keys, documented)
    assert missing == []
    assert extra == set()


def test_compute_drift_canonical_satisfies_alias_check(checker):
    """Inverse — canonical name alone is also sufficient."""
    settings_keys = {"foo": {"FOO_NEW", "FOO_OLD"}}
    documented = {"FOO_NEW"}
    missing, extra = checker.compute_drift(settings_keys, documented)
    assert missing == []
    assert extra == set()


def test_compute_drift_extra_keys_reported_but_not_failing(checker):
    """Keys in .env.example with no matching Settings field land in
    `extra` for visibility — but they're an info note, not a fail.
    Operators sometimes document platform vars (PORT, HOSTNAME) that
    pydantic-settings doesn't see."""
    settings_keys = {"foo": {"FOO"}}
    documented = {"FOO", "PLATFORM_VAR"}
    missing, extra = checker.compute_drift(settings_keys, documented)
    assert missing == []
    assert extra == {"PLATFORM_VAR"}


def test_compute_drift_multiple_missing_sorted_alphabetically(checker):
    """`missing` is sorted so CI failure output is deterministic — diffs
    across runs don't reshuffle the list."""
    settings_keys = {"zebra": {"ZEBRA"}, "alpha": {"ALPHA"}, "mike": {"MIKE"}}
    documented: set[str] = set()
    missing, _ = checker.compute_drift(settings_keys, documented)
    assert missing == ["alpha", "mike", "zebra"]


# ---------------------------------------------------------------------------
# format_missing_line — operator-facing message quality
# ---------------------------------------------------------------------------


def test_format_missing_line_single_name(checker):
    """Field with only its canonical name renders a single-line fix hint."""
    line = checker.format_missing_line("jwt_secret", {"JWT_SECRET"})
    assert "JWT_SECRET" in line
    assert ".env.example" in line


def test_format_missing_line_alias_names_listed(checker):
    """Field with aliases lists ALL accepted names so the operator knows
    they can use either — and steers toward the canonical with a hint."""
    line = checker.format_missing_line(
        "acceleration_min_slope_old_usd",
        {"ACCELERATION_MIN_SLOPE_OLD_USD", "ACCELERATION_MIN_PRIOR_USD"},
    )
    assert "ACCELERATION_MIN_SLOPE_OLD_USD" in line
    assert "ACCELERATION_MIN_PRIOR_USD" in line
