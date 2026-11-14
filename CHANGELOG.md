---
doc_id: fusion-cowork/changelog
product: fusion-cowork
class: platform
entry: service
doc_type: changelog
title: {zh: "变更日志", en: "Changelog"}
audience: engineer
status: draft
version: 0.5.99
updated: 2026-11-09
owner: "@dahai"
source_of_truth:
  - CHANGELOG.md
  - pyproject.toml
---

# Changelog

## v0.5.112 (issue #480 — mandatory verify_objective node)

- **verify_objective node (#480, PR #483)**: `nodes/ops/verify_node.py` — explicit deliverable-vs-top-level-objective verification as a DAG node (MAST FC3: explicit verification beats implied role prompts); `strategy=keyword` (deterministic case-insensitive rubric containment, zero-LLM, CI-safe) or `strategy=llm` (chat judge, JSON per-item verdict, conservative False on parse miss); `min_ratio` gate (default 1.0), `on_fail=fail` (node FAILED) / `flag` (SUCCESS with passed=false for branch routing); loud FAILED on missing rubric/objective/deliverable; Chinese aliases 目标校验/交付验证
- **Workflow validation policy (#480)**: `Workflow(verification_policy=...)` — `strict` = validate() errors when terminal deliverable chains lack a verify_objective predecessor; `lenient` (default) = warning only; `off` = silent; serialized via to_dict/from_dict
- **ApprovalJournal feed (#480)**: verify_objective outcomes recorded as decided journal rows (action_id `verify:{exec_id}:{node_id}`, params carry objective + rubric, approved = verdict) for audit
- Tests: `tests/test_issue_480_verify_objective.py` (20 cases: node strategies/thresholds/on_fail, validation policy matrix, journal feed)

## v0.5.111 (issues #470/#478/#479 — kernel checkpoint binding, workflow ladder, pipeline fixes)

- **Kernel checkpoint binding (#470, PR #481)**: `engine/kernel_checkpoint.py::KernelCheckpointBinder` wraps harness `CheckpointStore` (pre-execution state per DAG node, `session_id = run_id`, metadata carries node_id/node_name/params) + `ReceiptCache` (committed-node receipts keyed `{run_id}:{node_id}`); `WorkflowEngine(kernel_checkpoints=...)` opt-in seam — same-execution_id re-dispatch replays committed nodes as SKIPPED steps instead of re-executing effects (idempotent zombie resume); default None = zero behavior change
- **Workflow ladder node (#470, PR #481)**: `workflow_ladder` ecosystem node — staged worker ladders on harness `ParallelTeam` (stage-by-stage sequential, workers parallel), stage summaries feed the next stage (`propagate_context`), failure propagation, `allow_partial`, per-stage timeout, loud harness-missing error; HIGH_RISK_NODES + aliases 阶梯编排/工作流阶梯
- **String require_review gate fix (#479, PR #481)**: approval_gate auto-pass coerces `require_review` through the registry bool coercion — template param substitution renders booleans as the string `"false"`, which strict `is False` missed, so headless synthetic runs paused at every gate
- **Pipeline interpolation fixes (#478, PR #481)**: `${step.*}`/`${result.*}` refs accept an optional `|literal` fallback (missed paths degrade instead of leaving unresolved refs); product_lifecycle n3/n9 artifact-id handoff falls back to inline report when seats produce no artifacts; n12 `issue_number` references `${step.n6.result.number}` explicitly (ambient `${result.number}` resolved against the nearest upstream envelope and broke)

## v0.5.110 (issues #466/#470-scope follow-ups — CI gate, docs, export hardening)

- **Orchestration benchmark gates CI (#466 final piece, PR #475)**: new `benchmark-orchestration` job runs `fusion-cowork benchmark orchestration` on every code PR — the 6 deterministic scenarios must score 100 or the run fails (evolution plan W8 acceptance: benchmark runs in CI alongside the test matrix)
- **Admin audit export hardened (PR #476)**: `/api/admin/audit/export` now emits records in strict chronological order (audit rows + run.finished sorted by `time_unix_nano`) and the run-view assertion no longer depends on record position — export order is stable regardless of store write timing
- **Test fixture isolation (PR #474)**: the admin-takeover test client injects a tmp_path-isolated `space_data_dir` so assertions no longer read ambient audit rows from the shared default SpaceStore on machines running a live web serve
- **Evolution plan doc landed (PR #458)**: `docs/evolution-plan-2026-11.md` merged into the repo (docs-contract frontmatter included)

## v0.5.109 (issues #459/#460/#461/#462/#463/#464/#465/#466/#467 — evolution plan 2026-11 batch)

- **Inbound triggers (#459, PR #468)**: token-authed webhooks start runs — trigger CRUD (`GET/POST /api/triggers`, `DELETE /api/triggers/{id}`, token masked at rest) + `POST /api/hooks/trigger/{token}` (202, `compare_digest` token check, `/api/hooks/` prefix exempt from static key) resolves the bound workflow and launches a run with trigger params overlaid by the JSON payload; `wait_for_event` node parks the run mid-workflow until a matching external event resumes it (timeout = visible failure)
- **Session checkpoint (#460)**: step snapshots carry `session_id` — resume adopts the original `SessionStore` session instead of creating a new one, and the adopted session id is injected into downstream node inputs as `_session_id` (agent seat nodes continue their multi-turn history across resume); old snapshots without `session_id` behave byte-identical
- **Cross-run concurrency gate + spend attribution (#461, PR #469)**: `engine/run_gate.py` — named `concurrency_key` gates serialize resource-bound nodes across runs while distinct keys interleave (fair share); per-run wave concurrency; token/spend rollup per step and per run surfaced in run records
- **`sub_workflow` node (#462)**: nested DAG execution (`nodes/ecosystem/subworkflow_node.py`) with an isolated sub-execution and result propagation; cycle protection via depth budget
- **Durable HITL (#463)**: `engine/approval_journal.py` on sqlite — pending approvals survive serve restarts; HITL decisions from web and webhook now enter the tamper-evident audit hash chain (`hitl.resolve` audit entries, fire-and-forget with strong-ref task set + WARN on failure); timeout denials stay in the journal with `decided_by=timeout`; `SpaceStore.initialize()` double-run race guarded (asyncio.Lock + `_initialized` sentinel)
- **OTel trace spine (#464)**: `observability/trace.py` — `trace_id` = execution id, W3C `traceparent` parse/propagation, per-wave spans on the engine; upstream passthrough to fusion-mlx tracked as fusion-gateway#654 (out of repo per issues-only rule)
- **Wave-boundary mutation (#465)**: `remove_node` / `retarget_edge` run directives validated against the pending wave — structured 400 (unknown node/edge) and 409 (node not in pending wave) instead of silent drops
- **Orchestration benchmark (#466)**: `fusion-cowork benchmark orchestration` — 6 deterministic scenarios (linear chain, diamond fanout, retry-on-failure, wave parallel, sub-workflow nesting, type coercion) over registered probe nodes; score gate (100 required, CLI exit 1 otherwise) ready as a CI check
- **Coordinator fan-out + session fork (#467)**: `coordinator_fanout` runs workers as isolated fusion-harness `SubagentRunner` subagents (own session ids, narrowed tool allowlists, capped budget/turns, summary-only return; harness missing = loud FAILED pointing at `pip install 'fusion-cowork[harness]'`); `session_fork` branches the parent session into parallel child branches (`{parent}:fork:{name}`) with gather at the merge node — both HIGH_RISK gated, Chinese aliases registered (`协调分发`/`子代理分发`, `会话分叉`/`会话分叉并行`)
- Tests: `tests/test_issue_459_triggers_wait_event.py` … `tests/test_issue_467_fanout_session_fork.py` (issue-named files, one per workstream) + audit-chain wiring cases in `tests/test_issue_463_durable_hitl.py`

## v0.5.108 (issue #453)

- **HITL early-approval race fixed (`_hitl_await`)** — an approval arriving while the engine was still in its pre-wait awaits (hitl_pending NOTIFICATION hook, HITL email) had its wake signal wiped by the pause-event clear before the wait loop: the approved node was either auto-denied at HITL timeout or the run hung forever at the between-node W8 checkpoint with the node already executed (live signature: approved shell_exec stays blocked, run never finalizes). Decision/cancel check now runs at the top of every wait iteration and the event is cleared only when no decision exists (check + clear are await-free, atomic under the event loop); break-with-decision keeps the event set so the W8 checkpoint passes and the run finalizes normally. Regression test simulates the approval racing the hook fire (`tests/test_issue_453_tool_hitl_resume.py`).

## v0.5.107 (issue #452)

- **Demo contract gate (`demo_contract` node)** — AGENTS.md rule 7 "delivery is experience-able" wired into the delivery pipeline:
  - Three-piece validation (semantics mirrored from the meta-repo `check-demo-runnable.py`): `docker-compose.demo.yml` with per-service healthchecks, `demo.yaml` (web.port/terminal entry, optional mobile section: platform android|ios + artifact + stream), non-empty stub pack (`demo-stubs/` or `tests/fixtures/stubs/`). Missing pieces fail the delivery with an actionable error listing every violation
  - Bundle manifest output: slug, compose services, entry points, mobile section, stub dir + recorded fixture list — the payload the executor demo API consumes; optional meta-gate subprocess run (only violations naming this repo block; other repos warn), secret scan on stub pack (on by default) fails on credential-looking content — zero real customer data in the bundle
  - Opt-in executor registration (`register=true`): POST `{slug, kind, bundle}` to `<executor_url>/api/v1/demo/register` (`FUSION_EXECUTOR_URL`); unconfigured executor or non-2xx fails loudly. Executor-side e2e tracked with fusion-executor#68
  - `product_lifecycle` gains `n13 demo_contract` after close-out; `demo_gate` defaults false = success no-op (existing and synthetic-E2E runs byte-identical); empty `demo_deliverable_dir` resolves via `FUSION_BUSINESS_ROOT/<slug>`
- Tests: `tests/test_issue_452_demo_contract.py` (23 cases) + #307 template pin 14 → 15 nodes
- #451 (anonymous `/shell/` raw JSON) verified fixed live — gateway-side (`shellLoginRedirect`), closed with evidence, no cowork change

## v0.5.106 (issue #448)

- **Shell registry loop observability**: the registration loop logged bare exceptions with no target and spammed a WARNING every cycle during a gateway outage, and `/health` carried no registration signal — operators could not tell a config error (wrong host/port) from a gateway outage, and the failure rate was invisible. Fixes:
  - Loop startup logs the resolved gateway URL (`gateway=... module=... interval=...s`; admin key never logged)
  - Exceptions classified — `httpx.ConnectError` names the target (`connection refused/unreachable: <gateway>`), other httpx errors prefixed, unknown errors passed through
  - Invalid `FUSION_SHELL_GATEWAY_URL` fails fast at startup (ERROR log + no network loop) instead of looping forever
  - Sustained failure rate-limited: first 9 cycles keep per-cycle WARNINGs; from the 10th consecutive failure one ERROR summary per hour (`consecutive_failures` + target URL), suppressed cycles stay silent; recovery resets the counter
  - `/health` now carries `shell_registration` (`enabled` / `registered` / `gateway` / `last_error`) so supervisor/alerting can flag `registered=false`
- Tests: `tests/test_issue_448_registry_observability.py` (11 cases: URL validation, fail-fast, error classification, health transitions, hourly ERROR rate limit, recovery reset, /health envelope)

## v0.5.105 (issues #442/#443/#444/#445)

- **Gate approval semantics (#442)**: approving an `approval_gate` HITL card without `once:true` woke the run but the post-wake permission recheck denied it — #340 deliberately does not persist gate approvals as rules, so the recheck always refused. Gate tools (`HITL_GATE_TOOLS`) now skip the recheck on explicit approval; non-gate nodes keep the recheck path unchanged
- **Resume at a gate re-arms the HITL wait (#445)**: pause/resume shares the pause event with the HITL wait — resuming a run parked at a gate woke the wait with no recorded decision, treated as a denial (step 0.000s, run failed) before any approval could land. The wait now re-arms when woken without a decision or cancel, preserving the remaining #187 deadline; a later confirm wakes the run normally
- **Base-verdict leak fix (#444)**: base-verdict step output no longer carries the raw `matched` object (other customer's slug/repo leaked verbatim through `GET /api/runs/{id}`); `matched` is now `{}` for base, same policy as `matched_visible`. Template interpolation unaffected
- **Seat requirement wiring (#443)**: `product_lifecycle` n1 BA seat input embeds `${param.task_title}` + `${param.task_prompt}` — the seat previously received only the slug and hallucinated an unrelated PRD; n3/n9 already carry PRD/arch content (#346)
- Tests: `tests/test_issue_442_445_hitl_batch.py` (9 cases) + #422 base assertion updated

## v0.5.104 (issue #439)

- **CJK matching fix in `historical_match`**: contiguous CJK runs tokenized as a single token, so Chinese requirements never matched Chinese capability lists (score always 0.0, verdict always `new`). CJK runs now segment into character bigrams (single chars kept as-is) before scoring; latin/digit tokenization and the deterministic zero-LLM containment contract unchanged; stopword filtering stays symmetric on both sides
- Tests: `tests/test_issue_439_cjk_match.py` (17 cases: bigram segmentation, single-char runs, mixed EN/CN, partial/zero overlap, extend/new verdicts, English regression)

## v0.5.103 (issue #427)

- **Extension-run traceability (remaining in-repo scope of #427)**:
  - `historical_match` emits `extension_label` (`extension` when verdict=extend, empty otherwise) and `extension_contract` (deliverables for extend runs: PR label `extension`, a CHANGELOG top-level `## Extension Log` entry listing the capabilities added, PR body stating it extends the existing product); new/base verdicts emit empty strings so labels and the coder prompt stay byte-identical
  - `github_api` drops blank labels — the verdict-keyed extension label placeholder collapses cleanly on non-extension runs
  - `product_lifecycle`: n6 task issue carries the extension label placeholder, n7 coder prompt appends `extension_contract`
  - base-verdict extraction path deliberately out of cowork (cross-repo writes forbidden; needs the companion process on the multi-nodes side) — tracked on the issue
  - Tests: `tests/test_issue_427_traceability.py` (6 cases) + #307 label pin
  - Shipped earlier for #427: verdict card + re-verdict toggle (v0.5.101, PR #430)

## v0.5.102 (issue #431)

- **desigma-mcp integration — customer design docs flow into the delivery pipeline**:
  - `mcp_stdio` helper: newline-delimited JSON-RPC over a subprocess (initialize → notifications/initialized → tools/call sequence, per-request timeout, server always terminated incl. protocol errors). Spawn gated by `FUSION_COWORK_MCP_STDIO_ALLOW` — exact basename allowlist, empty/unset = deny all (fail-closed)
  - `mcp_client` node: new `transport=stdio` mode (`command`/`args` params); Streamable HTTP path untouched; JSON-string arguments still parsed (#211 semantics)
  - New `design_context` node: empty `workspace` → success no-op with empty `design_section` (zero-design runs byte-identical to prior behavior); with a workspace exports the doc structure (`desigma_get_document`) + node list (`desigma_list_nodes`) + per-language codegen (`desigma_generate_code`, frame auto-picked from the list when `frame_id=0`) into a full `design_section` block carrying the implement-from-design red line (deviations only where fd-design-lint flags)
  - `product_lifecycle`: `n4d` `design_context` step after gate A; n7 coder prompt appends `design_section` (empty string = prompt unchanged byte-for-byte); new params `design_workspace` / `design_command` / `design_frame_id` / `design_languages` / `design_timeout`, all defaulting to no-design behavior
  - `design_context` deliberately not in HIGH_RISK_NODES: the no-op path never spawns and the empty-default allowlist already refuses every command — high-risk classification would HITL-pause zero-design runs and break the unchanged-behavior red line
  - Tests: `tests/test_issue_431_desigma.py` (21 cases: allowlist, stdio protocol, node paths, template wiring, no-design regression) + #307 pin 13 → 14 nodes

## v0.5.101 (issue #422 F-1)

- **Match verdict card in run view + re-verdict (F-1 of the #422 conditional-pass acceptance)**:
  - `product_lifecycle` template: `n4m` `historical_match` moved before gate B (arch review) — edges now `n3 -> n4m -> n4 -> n5`; the pause at the confirm gate shows the completed verdict, matching the approved prototype intake flow (match auto-completes before the single human point)
  - Engine: new run directive action `override_output` — merges a dict into an already-executed node's output data so downstream `${step.<id>.<path>}` interpolation picks up the flipped verdict; unexecuted targets refused (fail-closed, `set_params` covers that case); a user-correction marker step lands on the timeline; web API `DIRECTIVE_ACTIONS` whitelists the action
  - Run view (`#/runs/<id>`): `historical_match` steps render as a dedicated verdict card — verdict badge (extend / base / new), match % vs threshold, capability gaps, candidate count; detail expand shows the matched project (extend) or the privacy note (base — name hidden per red line); "try existing project" opens the matched repo (extend only); "re-verdict: new project" submits `override_output` (extend/base only — `new` cannot claim a repo, `base->extend` would cross the customer red line); card display applies override payloads from `record.directives` so the flip shows immediately
  - Tests: 3 engine cases (override flows downstream + marker, unexecuted target refused, merge dict required), template wiring/#307 pin updated, 6 bun cases (`matchVerdictView` extraction, privacy fields, override application)

## v0.5.100 (issue #422)

- **Historical-project match before scaffolding (product_lifecycle)** — part 1 of #422, follow-ups in #427:
  - New `historical_match` node (ops): deterministic capability-overlap scoring (containment, zero LLM cost, replay-safe) over candidate projects (inline `candidates` or `candidates_file` JSON); three-way verdict — `extend` (same-customer candidate, downstream clones the existing repo) / `base` (other customer's candidate, matched details hidden — no cross-customer repo access or name disclosure) / `new` (fresh scaffold, prior behavior); `gaps` = required minus matched capabilities
  - Engine `skip_if` primitive: `NodeConfig.skip_if` conditions (`{source, path, equals|not_equals|in|not_in}`, all must match) evaluated after input interpolation and before the permission check — skipped nodes record `NodeStatus.SKIPPED` with the matched condition and never enter the HITL gate; missing source node fails closed
  - `repo_scaffold` extend mode: `verdict=extend` + `extend_repo` clones the matched repo (`--depth 50`) instead of scaffolding; GitHub token injected via `GIT_CONFIG_*` env only (never argv); existing non-empty worktree reused
  - `product_lifecycle` template: new `n4m` `historical_match` step between gate B (arch review) and scaffold; `verdict`/`matched_repo` wired into n5; new params `match_candidates` / `match_candidates_file` / `match_threshold` (0.6) / `own_customer` / `required_capabilities` — all default to prior always-scaffold-new behavior
  - Tests: `tests/test_issue_422_match.py` + `tests/test_issue_422_skip_if.py` (22 cases) + `test_issue_307` template pin updated to 13 nodes

## v0.5.99 (issue #387)

- **Synthetic E2E mode for the product_lifecycle pipeline** — zero real-model tokens, zero GitHub writes, unblocked by replay-dev going live (fusion-mlx#1518, deterministic zero-cost replay provider):
  - `github_api` gains `dry_run`: deterministic canned results per op family (`issue_create` / `linked_pr` / `pr_get`), still enforcing op validity + repo whitelist, no `GITHUB_TOKEN` required
  - `claude_dispatch` gains `dry_run`: no claude session spawn, deterministic pseudo-PR output, whitelist/issue_number/prompt/cwd checks still enforced
  - `studio_run` gains `model`: seat model pinning via `/v1/runs` body `spec.model` (synthetic mode pins `replay-dev`)
  - `product_lifecycle` template gains three synthetic switches, all default off (production behavior unchanged): `seat_model` -> n1/n3/n9 `model`; `gates_human` -> gate A/C `require_review` (gate B stays on the independent `review_arch` switch, #413); `node_dry_run` -> `dry_run` on n6/n7/n8/n11/n12
  - New nightly synthetic E2E workflow (`.github/workflows/nightly-synthetic.yml`) on the self-hosted mac runner (`synthetic` label): preflight the four local services, seed allow-rules for `studio_run`/`claude_dispatch`, launch the synthetic run through the expert channel, poll to terminal, assert every step succeeded; scheduled nightly + `workflow_dispatch`
  - Tests: `tests/test_issue_387_synthetic.py` (17 cases)

## v0.5.98 (issues #413/#416/#418/#409)

- feat: intake gains the architecture-review choice — `launch-intake` accepts `review_arch` (default false, 不评审 = auto-pass); `approval_gate` gains `require_review` and when false the engine skips the permission/HITL check and the node output records `auto_passed`/`review_arch`; TemplatesPage intake form gains the no-review / review-required radio; gates A and C untouched (#419).
- fix: repo_scaffold no longer doubles the `ai-` prefix on intake slugs (dir/package/title use the normalized slug) (#419).
- feat: intake repo owner defaults to the `dahai80-business` org (owner decision 2026-11-09), overridable via `FUSION_BUSINESS_GITHUB_OWNER` (#419).
- fix: studio_run default upstream wait 900s -> 1800s with a template-level `studio_wait_timeout` (default 1800) wired into all three seats; github_api issue_create dedupe search q truncated to the 256-char API limit, long task titles no longer 422 (#419).

## v0.5.97 (issues #404/#410/#414)

- feat: HITL email notification at every human touchpoint — every approval gate / agent-node permission pause emails the responsible user at their fusion-identity registered address (no new email storage; recipient resolved from the run's identity token via read-only `IdentityClient.get_user_email`, `FUSION_COWORK_NOTIFY_EMAIL` override). SMTP env-injected only (`SMTP_HOST/PORT/USER/PASS/FROM/TLS` — absent = disabled), fire-and-forget daemon thread with 3 attempts, failure logs a WARN and never blocks the pipeline; plain-text English body with run_id, node, `#/pipeline/<run_id>` deep link; resolved acks opt-in via `SMTP_NOTIFY_RESOLVED=1`. Live-verified end-to-end against a local SMTP sink (pending + resolved mails, correct body/deep link) (#411).
- fix: resume-from 500 on template-sourced runs — `RunRegistry.resume_from` assumed `workflow_def.nodes` is a list; template runs persist nodes as a dict, so every resume of an intake/template run raised `AttributeError`. Both shapes accepted (#411).
- fix: intake launch doubled the `ai-` slug prefix on template scaffold paths (`business/ai-ai-<name>`) — the template's `slug` param is the bare product slug; launch-intake now strips the prefix for the template param only, registry/repo/labels keep the prefixed form (#415).

## v0.5.96 (issue #400)

- feat: delivery-pipeline intake mode — the customer provides exactly one thing, a requirement text; everything else is derived server-side. New `fusion_cowork/server/intake.py`: deterministic slug derivation (ASCII words from the first line, CJK falls back to a content sha1), collision-safe `-2` suffix against `business/registry.yaml`, idempotent private-repo ensure via the GitHub API (existing repo = reuse), textual registry insertion that preserves comments (`source: pipeline` + run_id backfill), and a check-slugs gate that must exit 0 before any repo creation. `POST /api/templates/{id}/launch-intake` (dry_run preview 200 / launch 202, `source=intake` runs) + TemplatesPage intake panel with the explicit-params form moved under Advanced. Token discipline: `GITHUB_TOKEN` env only, never request body, never logged (#405).

## v0.5.95 (issue #399)

- fix: `RunRegistry.list()` falls back to persisted terminal summaries — after a service restart `/api/runs` returned an empty list while run history was on disk; the list now merges the latest 100 persisted summaries with live runs (memory wins, newest-first, `space_id` filter honored). run_summaries schema v4 adds `space_id` (v3 databases migrate via idempotent ALTER). Verified live on :11461 — the restart that previously blanked the run-history GUI now returns the pre-restart runs (#401).

## v0.5.94 (issues #386/#388/#389/#391/#395)

- feat: n8n workflow importer v1, import-only with a runnability guarantee — per-node coverage report (mappable / mappable_with_warning / unsupported, never silently dropped), n8n expression subset -> `${step...}` translation (shared contract fixtures, IF/Set operands included, contains direction fixed), 0903 canvas schema output, import vs imported_verified status honesty, two genuine community exports as real-world fixtures + verify CLI (#397).
- feat: run source/labels persisted + terminal run summaries (#390).
- feat: pipeline node-boundary contract test pack (#392).
- test: fault-injection chaos suite — seven scenarios (kill boundary, rerun scope, upstream 503, cancel long-gen, placeholder mismatch, budget park, deploy drift) (#393).
- test: vendored agent-studio run API contract fixtures consumed by studio_run tests (#396).

Entries are distilled from git tags and merge-commit subjects; full per-issue notes live in the referenced issues and the git history. Format: Keep a Changelog, versions are git tags.

## v0.5.93 (issue #382)

- feat: transactional outbox — fusion-harness kernel (harness#11) wired into the engine: one-transaction node-result + effects journal, idempotent GitHub write receipts, boot reconcile, opt-in effect worker with persisted backoff (#384).

## v0.5.92 (issue #378)

- fix: node-resolved watchdog floor — `BaseNode.watchdog_floor()` lets param-driven deadlines (claude_dispatch) raise the engine watchdog; engine never kills a node before its own deadline (#379).
- fix: dispatch timeout detach carries explicit `outcome=detached_session_still_running` + pid; cancel log shows both timeouts.

## v0.5.91 (issue #373)

- fix: product_lifecycle seat_max_turns default 24 → 48 (#376).
- fix: studio_run report-only recovery run on "Max iterations" budget exhaustion, on by default; upstream causes filed at fusion-agent-studio#640.

## v0.5.90 (issue #372)

- fix: crash-pause evidence — recovery logs the node executing at crash time (#374).
- fix: honest resume — dead-task resume relaunches from the success-step snapshot instead of fake-running.
- fix: stall watchdog — running run with no events for FUSION_COWORK_RUN_STALL_SECONDS (default 300) is crash-paused with cause.

## v0.5.89 (issue #368)

- fix: all-state issue dedupe — `issue_create` dedupe searches open+closed issues, delivered-and-closed task issues reused on reruns (#369).
- fix: `repo_scaffold` overwrite non-destructive — fills missing scaffold files only, never rewrites existing files.
- fix: no-op delivery contract — dispatch session closing the issue as duplicate/invalid succeeds (`closed_no_pr`); `linked_pr` require-gate passes closed issue without PR.

## v0.5.88 (issue #363)

- fix: explicit seat turn budget — studio_run gains a `max_turns` param (forwarded as `spec.max_turns` on the /v1/runs body); product_lifecycle exposes `seat_max_turns` (default 24) into all three studio_run seats; studio's 8-turn default no longer starves multi-artifact roles (#366).

## v0.5.87 (issues #361/#362)

- fix: template scaffold-overwrite threading — product_lifecycle exposes `scaffold_overwrite` (bool, default false) into n5 repo_scaffold, pipeline reruns no longer hard-fail on an existing work tree (#364).
- fix: linked_pr closing-link contract — `pr_link_strength` (closing|mention) computed from the PR body closing keyword; `require_closing_link` blocks mention-only (hygiene) PRs; claude_dispatch success output carries `session_claim` for HITL review; template `task_prompt` demands `Closes #N` and wires `require_closing_link` (default true) into n8 (#364).

## v0.5.86 (issue #358)

- fix: claude_dispatch timeout detaches the dispatch session instead of hard-killing it mid-delivery; engine node-watchdog cancellation detaches to the same watcher; default timeout 1800s -> 3600s with `FUSION_COWORK_CLAUDE_DISPATCH_TIMEOUT` override (#359).
- docs: adopt fusion docs-system v1.1 contract (platform x service) — six Tier 0/1 docs, frontmatter across in-scope markdown, CI validator workflow (#357).

## Unreleased

## v0.5.85 (issues #351/#352/#353, #356)

- fix: studio_run default timeout 900s + env override + detach instead of cancel (#354).
- fix: pipeline rerun idempotency + claude_dispatch permission mode (#355).

## v0.5.84 (issues #346/#347, #350)

- fix: product_lifecycle passes canonical artifact id into downstream seats.
- fix: claude_dispatch passes through claude auth env vars + crash output hardening.

## v0.5.83 (issue #343, #345)

- fix: result-ref envelope fallback + unresolved interpolation loud fail.

## v0.5.82 (issue #340, #342)

- fix: v0.5.82 patch (see issue #340 for details).

## v0.5.81 (issue #337, #339)

- fix: v0.5.81 patch (see issue #337 for details).

## v0.5.80 (issue #334, #336)

- fix: v0.5.80 patch (see issue #334 for details).

## v0.5.79 (issues #326/#327/#328, #333)

- fix: v0.5.79 patch (see issues #326/#327/#328 for details).

## Earlier

See git tags `v0.5.x` and the issue tracker (github.com/dahai80/fusion-cowork/issues) for the full history.
