"""Execution pipeline (PR D.3).

This package implements the SoDEX order lifecycle on top of D.1
(typed-data builders) + D.2 (HTTP clients). Production-side only —
NEVER signs. The wallet (frontend, wagmi/viem) supplies signatures
to `submit_new` / `submit_cancel`; this package forwards them to the
gateway and persists state.

Module layout (one-way dependency graph; leaves at the top):

```
pipeline/execution/
  bytes_helpers.py     # Pure — extract_params_bytes(payload_json)
  nonce.py             # next_nonce_for_user(session, user_id)
                       #   REQUIRES caller holds FOR UPDATE on user row
  symbols.py           # resolve_symbol_id(session, venue, asset)
                       #   SymbolNotResolved exception
  paper.py             # _simulate_fill() — paper-trade branch
  risk.py              # check_order(session, user, request) -> RiskDecision
                       #   D14 read-only, asserts FOR UPDATE held
  positions_spot.py    # spot_apply_fill, spot_reconcile_positions
  positions_perps.py   # perps_apply_fill, perps_reconcile_positions
  pipeline.py          # prepare_new + submit_new + prepare_cancel + submit_cancel
                       #   The orchestrator. D14 — caller commits.
```

Anti-drift rules (introduced by D.3 — see CLAUDE.md):
  - **28** — nothing here may import a signing primitive. Same grep
    test as rule 27, extended to `pipeline/execution/`.
  - **29** — all risk caps come from `settings.execution_*`. No
    hardcoded thresholds in `risk.py`.
  - **30** — the per-user `SELECT … FOR UPDATE` on the User row is
    the single concurrency primitive. Risk checks, nonce generation,
    and Order INSERT all happen inside it.
  - **31** — `Order.eip712_payload` is TEXT (byte-exact). Never round-
    trip through JSONB / json.loads on the write path. `bytes_helpers
    .extract_params_bytes` is the only sanctioned reader.
"""
