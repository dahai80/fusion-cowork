# Capability Catch-up & Beyond Plan (2026-11)

Source: `~/fusion/insight/fusion-cowork-evolution-2026-11.md` (industry survey: LangGraph / MAF / CrewAI / OpenAI Agents SDK / Temporal / Hatchet / DBOS / n8n / Dify, plus local code audit). This doc turns the survey's gap register (W1-W8) and three-phase goals into a concrete, sequenced execution plan for fusion-cowork.

## Baseline (what we ship today)

- Wave-scheduled DAG engine, step-level checkpoint (kill -9 recoverable), 84 nodes (macOS/CDP density), HITL approval queue, directive injection, webhook resume into existing runs, ~2,645 tests, issue-driven release cadence.
- Differentiated assets to defend: local-device + browser orchestration, ~320 Chinese aliases, harness-kernel single-source shims, memory bridge nodes.

## Gap register (severity from survey)

| # | Gap | Severity |
|---|---|---|
| W1 | Checkpoint excludes agent session state (step snapshots only) | P0 |
| W2 | No inbound event trigger that creates a run | P0 |
| W3 | No first-class SubWorkflow node | P1 |
| W4 | HITL approval not a durable signal (queue + 1800s timeout; mid-crash approval recovery unproven) | P1 |
| W5 | No concurrency keys / fair scheduling / per-run token+spend attribution | P1 |
| W6 | OTel not spanning workflow + fusion-mlx LLM + harness tool spans as one trace | P1 |
| W7 | No mid-run graph mutation (wave-boundary only) | P2 |
| W8 | No orchestration-quality benchmark in CI | P2 |

## Phase 1 — catch up to the 2026 production floor (0-3 months)

### 1.1 Inbound triggers (W2, ship first — smallest blast radius, highest user value)

- `trigger` registry: `POST /api/triggers` (webhook URL + workflow_id + optional param mapping + secret token), cron already exists via scheduler.
- Inbound webhook with valid token creates a run from the bound workflow definition; event-wait primitive (`wait_for_event` node) subscribes to trigger events keyed by name — parity with Hatchet event waits.
- Reuse existing webhook auth (`/api/hooks/webhook/{run_id}` signing) — no new auth surface.
- Acceptance: signed POST to trigger URL creates run within 1s; unsigned/late-token request 401; event-wait node resumes on matching event; tests issue-tracked.

### 1.2 Session-level checkpoint (W1, the core catch-up item)

- Extend run snapshot schema: step snapshots gain optional `session_ref` — conversation history, tool results, and harness funnel/compactor state for long agent seats (studio_run, claude_dispatch, space agent nodes).
- Resume semantics: crash mid-agent-node → resume restores the session mid-conversation, not from step boundary. Checkpoint contract stays backward compatible via SCHEMA_VERSION migration (defense line 3).
- Acceptance: kill -9 a run during a multi-turn agent node; resume continues the conversation with prior turns intact; old runs (no session_ref) resume unchanged.

### 1.3 Concurrency + spend baseline (W5)

- Default wave concurrency: make it configurable per run (`concurrency_key`), fair-share across runs instead of global 1.
- Per-run token/spend attribution from fusion-harness real token counts; surface `tokens`/`spend` on run detail + `/api/usage` breakdown by run/node.
- Acceptance: two runs with distinct keys interleave; usage API reports per-run and per-node spend; zero regression on single-run latency.

## Phase 2 — composition & trust (3-9 months)

### 2.1 SubWorkflow node (W3)

- `sub_workflow` node: child DAG executes as one node (param inheritance, output return, step-tree nesting in run view), enabling template layering.
- Acceptance: nested run appears as a single wave step with expandable child steps; failure/cancel propagates; depth cap enforced.

### 2.2 HITL durable approval (W4)

- Approvals become persisted resumable waits: approval decision journal (SQLite/Postgres) survives server crash; after restart, resume reopens the pending approval instead of timeout-dropping it. Decisions enter the audit chain (`security/audit.py`).
- Acceptance: create pending approval, kill -9 server, restart, approve — run continues; audit log records decision with attribution.

### 2.3 OTel spine (W6)

- One trace across: workflow span (engine) ↔ fusion-mlx LLM spans (via gateway trace headers) ↔ harness tool spans. Eval scores (fusion-eval) attach to run traces. Trajectory recorder and OTel export converge on one trace_id.
- Acceptance: single trace visible end-to-end in OTLP collector for a 3-node run with one LLM seat.

### 2.4 Wave-boundary dynamic mutation (W7)

- Extend `directive` API: insert/remove/re-target nodes at wave boundaries only (defense line 4 — no mid-wave mutation).
- Acceptance: directive-driven node insertion lands in the next wave; illegal mid-wave requests rejected with structured error.

## Phase 3 — surpass (9-18 months)

Where we do not just catch up but lead:

1. **Orchestration-quality benchmark in CI (W8)** — WorkflowArena (arXiv 2611.12953) adaptation or self-built executable orchestration eval, run in CI alongside fusion-bench. First-mover: cross-framework orchestration evals are an open slot; a local-first benchmark nobody else has.
2. **Coordinator-subagent fan-out primitive** — node-level coordinator/worker pattern on fusion-harness subagent orchestration (parity with Temporal multi_agent / OpenAI handoffs), but with wave scheduling + HITL gates + device nodes that cloud frameworks lack.
3. **Session fork** — one agent session branches into parallel child branches each holding its own session id (Temporal `fork_session` parity), composed with our wave model: fork at wave boundary, gather at merge node.
4. **Local-first durable agent runtime as the niche** — the combination no competitor ships: durable session-level checkpointing for on-device agents driving macOS/CDP automation. n8n/Dify have neither device density nor durable sessions; Temporal has durability but no device layer.
5. **Governance layering** — orchestration core stays open; audit/quotas/tenancy ride fusion-security/fusion-identity. Mirrors the industry "free core + commercial control plane" split without splitting the repo.

## Defense lines (unchanged from survey, restated as review gates)

1. Local-device/browser orchestration niche — no refactor degrades it toward a generic cloud canvas.
2. Harness kernel single source — engine shims stay thin re-exports; no permission/funnel/compactor re-implementation in cowork.
3. Checkpoint contract backward compatible — session-level extension via SCHEMA_VERSION migration only.
4. Wave semantics — dynamic changes at wave boundaries only, never mid-wave.
5. Chinese aliases + issue-driven tests — every new node ships with aliases and issue-named tests.

## Execution mechanics

- One issue per workstream (W1-W8 mapped), each workstream lands via its own branch → PR → CI 25-check gate → patch release, matching the standing release cycle.
- Phase 1 order: 1.1 triggers → 1.3 concurrency/spend → 1.2 session checkpoint (largest; lands last in phase so schema migration rides a dedicated release).
- Every phase item ends with a live dual-channel deploy verification (desk :11438 / web :11461) before the issue closes.
- Risks: session checkpoint touches snapshot schema (mitigate with migration tests + old-run fixture suite); OTel spine depends on fusion-gateway trace-header passthrough (coordinate via upstream issue, no code change in gateway per scope rule).
