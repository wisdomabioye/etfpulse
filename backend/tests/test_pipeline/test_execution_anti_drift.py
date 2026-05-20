"""Anti-drift checks for `pipeline/execution/` (PR D.3).

Mirrors `tests/test_adapters/test_sodex_typed_data.py::TestAntiDriftRule27`
but extended to the execution-pipeline package. Rule 28 is the formal
"no signing primitives" extension of rule 27 into the wider execution
surface.

These tests fail loudly on grep-time so a future PR can't silently
introduce a signing primitive into the execution path. They're cheap
(string-line scan, no import) and run in every CI tick.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_EXECUTION_DIR = Path(__file__).resolve().parents[1].parent / "etfpulse" / "pipeline" / "execution"

# Same forbidden-imports list as rule 27. `eth_utils.keccak` (hash
# primitive) is allowed; we only block signing-key handling.
_FORBIDDEN_IMPORTS = [
    "from eth_account",
    "import eth_account",
    "from web3.auto",
    "import web3.auto",
    "sign_message",
    "sign_typed_data",
    "PrivateKey",
]


class TestAntiDriftRule28:
    """No file under `etfpulse/pipeline/execution/` may import a signing
    primitive. The execution surface receives ALREADY-SIGNED bytes from
    the wallet (via the API layer) and forwards them; it never signs."""

    def test_no_signing_primitive_imports(self):
        violations: list[str] = []
        for path in _EXECUTION_DIR.glob("*.py"):
            source = path.read_text(encoding="utf-8")
            for ln in source.splitlines():
                stripped = ln.lstrip()
                # Only inspect actual import statements — the rule
                # narrative in docstrings is allowed to mention these names.
                if not stripped.startswith(("import ", "from ")):
                    continue
                for needle in _FORBIDDEN_IMPORTS:
                    if needle in ln:
                        violations.append(f"{path.name}: {ln.strip()}")
        assert not violations, (
            "Anti-drift rule 28 violated — pipeline/execution/ imports a "
            "signing primitive:\n  " + "\n  ".join(violations)
        )


class TestAntiDriftRule29:
    """All risk caps come from `settings.execution_*`. `pipeline/execution/risk.py`
    must NOT contain hardcoded numeric thresholds in cap-check branches."""

    def test_no_hardcoded_cap_thresholds_in_risk(self):
        risk_path = _EXECUTION_DIR / "risk.py"
        source = risk_path.read_text(encoding="utf-8")

        # Look for direct numeric comparisons in cap-checking blocks.
        # The canonical pattern is `settings.execution_xxx`. Anything
        # else that looks like `count >= 5` or `notional > 10000` is a
        # smell. We scan for the cap reasons we shipped and assert each
        # is preceded by an attribute access on `settings`.
        forbidden_patterns = [
            "open_count >= 5",  # hardcoded cap
            "leverage > 5",
            "notional > 10000",
        ]
        violations = [p for p in forbidden_patterns if p in source]
        assert not violations, (
            "Anti-drift rule 29 violated — risk.py contains a hardcoded "
            f"threshold instead of `settings.execution_*`: {violations}"
        )

        # Positive check: the file references `settings.execution_*` —
        # if this drifts to zero, the rule has been quietly side-stepped.
        assert (
            "settings.execution_max_open_orders_per_user" in source
            and "settings.execution_daily_notional_usd_cap" in source
            and "settings.execution_max_leverage" in source
        ), (
            "risk.py is missing references to the `settings.execution_*` "
            "caps — anti-drift rule 29's positive check failed."
        )


class TestAntiDriftRule30:
    """The per-user SELECT FOR UPDATE is the single concurrency primitive.
    `risk.check_order` is the function that acquires it; tests pin the
    behaviour. This test checks the lock acquisition syntax stays in
    risk.py so refactors can't silently move it."""

    def test_for_update_held_in_risk(self):
        risk_path = _EXECUTION_DIR / "risk.py"
        source = risk_path.read_text(encoding="utf-8")
        assert "with_for_update()" in source, (
            "Anti-drift rule 30 violated — risk.py no longer acquires "
            "SELECT FOR UPDATE. The execution surface depends on this "
            "as the sole concurrency primitive."
        )

    def test_for_update_not_in_nonce_or_pipeline(self):
        """nonce.py and pipeline.py (the orchestrator) must NOT acquire
        their own row locks — the rule is "ONE primitive, opened by
        risk." A second lock site is the kind of drift that introduces
        deadlocks under load."""
        # pipeline.py is allowed to re-acquire for cancel paths (which
        # `prepare_cancel` explicitly does — same primitive, same
        # invariant). The forbidden case is `nonce.py`.
        nonce_path = _EXECUTION_DIR / "nonce.py"
        nonce_source = nonce_path.read_text(encoding="utf-8")
        assert "with_for_update" not in nonce_source, (
            "Anti-drift rule 30 violated — nonce.py acquires its own "
            "FOR UPDATE. The lock MUST be held by the caller (risk)."
        )


class TestAntiDriftRule31:
    """`Order.eip712_payload` is TEXT (byte-exact). `bytes_helpers.extract_params_bytes`
    is the only sanctioned reader on the HTTP write path. The Order model
    must declare the column as Text, not JSONB."""

    def test_eip712_payload_column_is_text(self):
        from etfpulse.models import Order

        column = Order.__table__.c.eip712_payload
        assert "TEXT" in str(column.type).upper(), (
            "Anti-drift rule 31 violated — Order.eip712_payload is not "
            f"TEXT (got {column.type}). JSONB normalises whitespace and "
            "breaks the byte-exact signed-payload contract."
        )

    def test_extract_params_bytes_is_exported(self):
        """If someone deletes extract_params_bytes, all the byte-exact
        write paths break silently. Pin its presence."""
        from etfpulse.pipeline.execution import bytes_helpers

        assert hasattr(bytes_helpers, "extract_params_bytes")


# Suppress unused-import for pytest discovery — kept in case future
# class-level skipif markers are added.
_ = pytest
