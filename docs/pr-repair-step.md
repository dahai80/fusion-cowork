# PR Design: LLM-informed node repair after retry exhaustion

Branch: `exp/dynamic-orchestration` ｜ Author: AI Software Factory Researcher ｜ Date: 2026-11-26

## Motivation

Experiment orch-bench v2 (factory-software/reports/2026-11-26-orchbench-dynamic-vs-static.md) showed:

- Free-order LLM orchestration beats static DAG on unanticipated errors but collapses on policy adherence
  (strong-model capability inversion: 32B 25% vs 4B 83%).
- Hybrid architecture (static skeleton + code policy + LLM repair on error only) reached 100% on both models
  at ~1/64 the token cost of pure dynamic orchestration.

fusion-cowork's engine already has blind retry (`NodeConfig.max_retries/retry_delay/retry_on_business_failure`)
but no LLM-informed repair. This PR adds the missing piece without touching static topology or deterministic policy.

## Semantics

New `NodeConfig.on_error_repair: bool = False` (default OFF, zero behavior change, opt-in per node).

After the existing retry loop ends with `NodeStatus.FAILED` and `on_error_repair=True`:

1. One LLM call (via the engine's `FusionMLXClient` seam) with a compact JSON prompt:
   node name, sanitized params, input_data, error, attempt_errors list.
2. LLM replies `{"action": "retry"|"skip"|"abort", "params": {...}}` (strict `_parse_json`, default `abort` on parse failure).
3. `retry` — re-execute the node once with repaired params (repair is one-shot; no LLM on the second failure).
4. `skip` — mark step `SKIPPED` with `summary="llm_repair: skip"`, run continues (equivalent to one-shot continue_on_error).
5. `abort` — fail exactly as today (current code path).

Guarantees:

- Repair never bypasses the permission check: repaired params re-enter the same `_permission_manager.check` path on re-execution.
- Repair decision is logged (`[<run_id>] llm_repair: node=<name> action=<action>`) and recorded in the step (`attempt_errors` unchanged, new `output_data["_repair"]` marker).
- Default OFF everywhere; no template changes.

## Implementation plan

1. `engine/node.py`: `NodeConfig.on_error_repair` + to_dict/from_dict.
2. `engine/workflow.py`: `_llm_repair_decision()` helper (seam-injectable callable, defaults to FusionMLXClient; tests inject a stub);
   hook after the `result is None`/FAILED finalization (~L1869), before rollback/attribution so backend rollback stays last.
3. Engine constructor seam: `llm_repair_client=None` (lazy FusionMLXClient on first use).
4. Tests `tests/test_repair_step.py`:
   - repair retry succeeds with modified params (stub client)
   - repair skip continues run, downstream consumes upstream outputs
   - repair abort preserves today's failure path byte-for-byte (default-off regression)
   - parse-failure fallback = abort
   - permission denial on repaired params still gates (mock deny)
   - one-shot guarantee: second failure does not invoke LLM again
5. `CLAUDE.md` claim tags: `CLAIM: verified by tests/test_repair_step.py`.
6. Lint: `ruff check fusion_cowork/ tests/` clean, line-length 120, no docstrings.

## Out of scope

- Distilling repair traces into static workflows (ReTrace-style; future PR).
- Any change to the main-loop topology or directive system.

## Status

- [x] Design
- [x] Implementation (engine/node.py config + engine/workflow.py one-shot repair hook, seam `repair_client`)
- [x] Tests (tests/test_repair_step.py, 12 cases: retry-with-params / skip / abort default-off regression / parse+LLM failure fallback / one-shot / config roundtrip)
- [x] ruff clean, full pytest green (exit 0)
- [ ] PR opened against main
