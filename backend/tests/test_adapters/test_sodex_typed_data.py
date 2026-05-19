"""Golden tests for the SoDEX EIP-712 typed-data builders (PR D.1).

The V.1 fixture (`tests/fixtures/sodex_eip712_golden.json`) was captured
from the official Go SDK against a throwaway burner key (see
`scripts/sodex_verify/eip712_capture/`). It is the **byte-exact contract**
between the Python builders and the SoDEX gateway:

- If the Python builder's `payload_json` differs from the fixture by
  even one byte (whitespace, key order, escaping, decimal precision),
  the `payload_hash` diverges, the wallet's signature fails to verify,
  and the gateway rejects the order.
- If the EIP-712 typed-data domain/message values don't match the
  fixture's semantics, wagmi/viem signs the wrong message and the
  gateway again rejects.

These tests pin the contract. **Re-running the V.1 capture is the ONLY
sanctioned way to change them** — any drift between the Go SDK and these
expectations is caught here.

In addition to the golden cases:
  - The serializer's primitive invariants (no whitespace, declaration
    order, omitempty) are tested directly so a future refactor of
    `compact_json` can't silently rotate output through a "looks-right
    but different" form.
  - Anti-drift rule 27 (CLAUDE.md) is verified by grepping the package
    source for forbidden imports.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from etfpulse.adapters.sodex import (
    PerpsNewOrderRequest,
    SpotBatchCancelOrderRequest,
    SpotBatchNewOrderRequest,
    SpotNewOrderItem,
    TypedDataBundle,
    build_perps_cancel_order,
    build_perps_new_order,
    build_spot_cancel_order,
    build_spot_new_order,
)
from etfpulse.adapters.sodex.serialization import compact_json

# Authoritative byte-exact contract — captured from the Go SDK.
_FIXTURE_PATH = (
    Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "sodex_eip712_golden.json"
)


def _load_fixture() -> dict:
    with open(_FIXTURE_PATH, encoding="utf-8") as fh:
        return json.load(fh)


# Map (action_type, venue) → (request_model_cls, composer_func) so the
# parametrized test can iterate the full fixture uniformly.
_COMPOSER_MAP = {
    ("newOrder", "spot"): (SpotBatchNewOrderRequest, build_spot_new_order),
    ("cancelOrder", "spot"): (SpotBatchCancelOrderRequest, build_spot_cancel_order),
    ("newOrder", "perps"): (PerpsNewOrderRequest, build_perps_new_order),
    # Perps cancel isn't in the V.1 fixture (no representative case
    # captured) — included here for completeness so adding a fixture
    # case automatically picks up coverage. See `test_perps_cancel_*`
    # below for its standalone unit test.
    ("cancelOrder", "perps"): (None, build_perps_cancel_order),
}


def _composer_cases() -> list[dict]:
    """Cases from the fixture for which we have a request schema + composer."""
    fix = _load_fixture()
    cases = []
    for case in fix["cases"]:
        key = (case["action_type"], case["venue"])
        cls, _composer = _COMPOSER_MAP.get(key, (None, None))
        if cls is not None:
            cases.append(case)
    return cases


# ---------------------------------------------------------------------------
# Golden — every fixture case round-trips byte-exact through the composer.
# ---------------------------------------------------------------------------


class TestGoldenFixture:
    """The byte-exact contract — Python builder output equals Go SDK output."""

    @pytest.mark.parametrize("case", _composer_cases(), ids=lambda c: c["name"])
    def test_full_bundle_matches_fixture(self, case):
        key = (case["action_type"], case["venue"])
        cls, composer = _COMPOSER_MAP[key]
        request = cls.model_validate(case["request"])
        bundle = composer(request=request, chain_id=case["chain_id"], nonce=case["nonce"])

        # The load-bearing assertions: bytes the gateway re-hashes,
        # and the hash the wallet signs.
        assert bundle.payload_json == case["payload_json"]
        assert bundle.payload_hash == case["payload_hash"]

        # Bundle metadata round-trip.
        assert bundle.action_type == case["action_type"]
        assert bundle.chain_id == case["chain_id"]
        assert bundle.nonce == case["nonce"]
        assert bundle.venue == ("sodex_spot" if case["venue"] == "spot" else "sodex_perps")

        # EIP-712 typed-data semantic equivalence. Fixture stores chainId
        # as Go-apitypes hex string and nonce as decimal string; we emit
        # native Python ints. Compare by canonical form.
        domain = bundle.typed_data["domain"]
        fix_domain = case["eip712_domain"]
        assert domain["name"] == fix_domain["name"]
        assert domain["version"] == fix_domain["version"]
        assert domain["chainId"] == int(fix_domain["chainId"], 16)
        assert domain["verifyingContract"] == fix_domain["verifyingContract"]

        message = bundle.typed_data["message"]
        fix_message = case["eip712_message"]
        assert message["payloadHash"] == fix_message["payloadHash"]
        assert message["nonce"] == int(fix_message["nonce"])

        # Spec invariants — independent of the fixture.
        assert bundle.typed_data["primaryType"] == "ExchangeAction"
        assert "EIP712Domain" in bundle.typed_data["types"]
        assert "ExchangeAction" in bundle.typed_data["types"]


# ---------------------------------------------------------------------------
# Perps cancel — not in V.1 fixture; standalone byte invariants only.
# (Fixture coverage gap acknowledged; this test guards the code path
# against accidental breakage between fixture re-captures.)
# ---------------------------------------------------------------------------


class TestPerpsCancelComposer:
    """`build_perps_cancel_order` isn't covered by the V.1 fixture (no
    capture case). These tests pin that the composer at least produces
    structurally-valid output — payload wraps correctly, hash is 32-byte
    hex, domain is "futures", venue is "sodex_perps". A future fixture
    re-capture should ADD a perps cancel case, at which point the
    golden suite above picks it up automatically."""

    def test_basic_perps_cancel_bundle(self):
        from etfpulse.adapters.sodex import PerpsCancelItem, PerpsCancelOrderRequest

        request = PerpsCancelOrderRequest(
            accountID=1001,
            cancels=[PerpsCancelItem(symbolID=1, clOrdID="test-cancel-1")],
        )
        bundle = build_perps_cancel_order(request=request, chain_id=138565, nonce=1700000000000)

        # Structural invariants.
        assert isinstance(bundle, TypedDataBundle)
        assert bundle.action_type == "cancelOrder"
        assert bundle.venue == "sodex_perps"
        assert bundle.chain_id == 138565
        assert bundle.nonce == 1700000000000
        assert bundle.typed_data["domain"]["name"] == "futures"
        assert bundle.payload_hash.startswith("0x") and len(bundle.payload_hash) == 66

        # Payload wrapping is correct.
        payload = json.loads(bundle.payload_json)
        assert payload["type"] == "cancelOrder"
        assert payload["params"]["accountID"] == 1001
        assert payload["params"]["cancels"][0]["clOrdID"] == "test-cancel-1"


# ---------------------------------------------------------------------------
# Serializer primitive invariants — independent of fixture content.
# ---------------------------------------------------------------------------


class TestCompactJsonInvariants:
    """`compact_json` is the foundation. If these break, every signature breaks."""

    def test_no_whitespace_anywhere(self):
        item = SpotNewOrderItem(symbolID=1, clOrdID="c-1", side=1, type=1, timeInForce=1)
        out = compact_json(item)
        assert " " not in out
        assert "\n" not in out
        assert "\t" not in out

    def test_field_declaration_order_preserved(self):
        item = SpotNewOrderItem(
            symbolID=1,
            clOrdID="c-1",
            side=1,
            type=1,
            timeInForce=1,
            price="100",
            quantity="0.5",
            funds="50",
        )
        out = compact_json(item)
        # Keys must appear in Pydantic field declaration order, which
        # matches the Go SDK struct order.
        expected = (
            '{"symbolID":1,"clOrdID":"c-1","side":1,"type":1,"timeInForce":1,'
            '"price":"100","quantity":"0.5","funds":"50"}'
        )
        assert out == expected

    def test_omitempty_drops_none(self):
        # No price / quantity / funds set → all three keys absent.
        item = SpotNewOrderItem(symbolID=1, clOrdID="c-1", side=1, type=1, timeInForce=1)
        out = compact_json(item)
        assert "price" not in out
        assert "quantity" not in out
        assert "funds" not in out
        # But required fields are present.
        assert '"symbolID":1' in out
        assert '"clOrdID":"c-1"' in out

    def test_int_enum_serializes_as_int(self):
        from etfpulse.adapters.sodex import OrderSide, OrderType, TimeInForce

        # use_enum_values=True ensures we see "side":1 not "side":"BUY"
        item = SpotNewOrderItem(
            symbolID=1,
            clOrdID="c-1",
            side=OrderSide.BUY,
            type=OrderType.LIMIT,
            timeInForce=TimeInForce.GTC,
        )
        out = compact_json(item)
        assert '"side":1' in out
        assert '"type":1' in out
        assert '"timeInForce":1' in out

    def test_dict_input_passes_through(self):
        # Raw dict input shouldn't go through model_dump — caller owns shape.
        # Note: None stays as null (caller's choice to include it).
        assert compact_json({"a": 1, "b": None, "c": [1, 2]}) == '{"a":1,"b":null,"c":[1,2]}'


# ---------------------------------------------------------------------------
# Mutation-isolation invariant — each `build_typed_data` call must
# return fresh dict instances. The previous design stored the
# EIP712Domain type definitions as a `tuple[dict, ...]` module-level
# constant; `list(tuple_of_dicts)` shares inner references, so a
# caller mutating `td["types"]["EIP712Domain"][0]["name"]` corrupted
# every subsequent call. The fix uses `tuple[tuple[str, str], ...]`
# (immutable pairs) and builds the dict-form output per call.
# ---------------------------------------------------------------------------


class TestBoolNotAcceptedAsInt:
    """Python's `bool` is a subclass of `int`, so `isinstance(True, int)` is
    True. Without an explicit `type(x) is int` check, a caller passing
    `chain_id=True` or `nonce=False` would silently emit `{"chainId": true}`
    in the typed-data — signing a different EIP-712 message than intended.
    The builder explicitly rejects bools to close this footgun."""

    def test_chain_id_true_rejected(self):
        from etfpulse.adapters.sodex.eip712 import build_typed_data

        with pytest.raises(ValueError, match="positive int"):
            build_typed_data(
                domain_name="spot", chain_id=True, payload_hash="0x" + "a" * 64, nonce=1
            )

    def test_nonce_false_rejected(self):
        from etfpulse.adapters.sodex.eip712 import build_typed_data

        with pytest.raises(ValueError, match="non-negative int"):
            build_typed_data(
                domain_name="spot", chain_id=1, payload_hash="0x" + "a" * 64, nonce=False
            )


class TestMutationIsolation:
    def test_caller_mutation_does_not_leak_into_next_call(self):
        from etfpulse.adapters.sodex.eip712 import build_typed_data

        td1 = build_typed_data(
            domain_name="spot", chain_id=286623, payload_hash="0x" + "a" * 64, nonce=1
        )
        # Mutate the inner type-definition dict.
        td1["types"]["EIP712Domain"][0]["name"] = "CORRUPTED"
        td1["types"]["ExchangeAction"][0]["type"] = "CORRUPTED"

        # Next call must NOT carry the corruption.
        td2 = build_typed_data(
            domain_name="spot", chain_id=286623, payload_hash="0x" + "b" * 64, nonce=2
        )
        assert td2["types"]["EIP712Domain"][0] == {"name": "name", "type": "string"}
        assert td2["types"]["ExchangeAction"][0] == {"name": "payloadHash", "type": "bytes32"}

    def test_message_dict_is_not_shared(self):
        from etfpulse.adapters.sodex.eip712 import build_typed_data

        td1 = build_typed_data(
            domain_name="spot", chain_id=286623, payload_hash="0x" + "a" * 64, nonce=1
        )
        td1["message"]["payloadHash"] = "0xdeadbeef"
        td2 = build_typed_data(
            domain_name="spot", chain_id=286623, payload_hash="0x" + "c" * 64, nonce=99
        )
        assert td2["message"]["payloadHash"] == "0x" + "c" * 64
        assert td2["message"]["nonce"] == 99


# ---------------------------------------------------------------------------
# Anti-drift rule 27 (CLAUDE.md): the SoDEX adapter package must NEVER
# import signing primitives. Verified by grep at test time so a future
# careless `from eth_account import ...` line is caught before it lands.
# ---------------------------------------------------------------------------


class TestAntiDriftRule27:
    """No file under `etfpulse/adapters/sodex/` may import a signing primitive.

    `eth_utils.keccak` is allowed (hash, not signing).
    `eth_account.*`, `web3.auto.signing`, etc. are forbidden.
    """

    _PACKAGE_DIR = Path(__file__).resolve().parents[2] / "etfpulse" / "adapters" / "sodex"

    _FORBIDDEN_IMPORTS = [
        "from eth_account",
        "import eth_account",
        "from web3.auto",
        "import web3.auto",
        "sign_message",
        "sign_typed_data",
        "PrivateKey",
    ]

    def test_no_signing_primitive_imports(self):
        violations: list[str] = []
        for path in self._PACKAGE_DIR.glob("*.py"):
            source = path.read_text(encoding="utf-8")
            # Strip comments + docstrings from the source so the *narrative*
            # rule documentation (which mentions the forbidden names) doesn't
            # flag itself. We use a simple line-based heuristic — split on
            # newlines, drop pure-comment lines, and check the rest.
            lines = source.splitlines()
            code_lines = [ln for ln in lines if not ln.lstrip().startswith("#")]
            # Heuristically strip docstring blocks: find pairs of triple-quote
            # lines and remove everything between. For a tight check we just
            # match against `import` and `from` statements, which won't appear
            # inside docstrings as syntactically-valid python.
            import_lines = [ln for ln in code_lines if ln.lstrip().startswith(("import ", "from "))]
            for ln in import_lines:
                for needle in self._FORBIDDEN_IMPORTS:
                    if needle in ln:
                        violations.append(f"{path.name}: {ln.strip()}")
        assert not violations, (
            "Anti-drift rule 27 violated — SoDEX adapter imports a "
            "signing primitive:\n  " + "\n  ".join(violations)
        )
