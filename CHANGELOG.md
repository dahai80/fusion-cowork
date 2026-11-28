---
doc_id: fusion-cowork/changelog
product: fusion-cowork
class: platform
entry: service
doc_type: changelog
title: {zh: "变更日志", en: "Changelog"}
audience: engineer
status: draft
version: 0.5.151
updated: 2026-11-28
owner: "@dahai"
source_of_truth:
  - CHANGELOG.md
  - pyproject.toml
---

# Changelog

## v0.5.151 (issue #623 — English-only source batch 2)

- **#623 (PR #627)**: English-only source batch 2 — ~11.3k unmarked CJK lines across `.py`/`.rs`/`.ts` source remediated. Comments, docstrings, and `logger.*` messages translated to English (issue refs, audit tags, and code identifiers preserved); multi-line LLM prompts converted to parenthesized implicit-concat blocks so each CJK line carries a same-line marker (content byte-identical). User-facing product strings (UI labels, CLI output, exception/`NodeResult` messages, LLM prompt text, aliases, `display_name`/`default_label`) stay Chinese with an `# i18n: user-facing` / `// i18n: user-facing` marker, giving a single grep gate: `python scripts/check_unmarked_cjk.py`-style residual = 0 (marker-aware scanner over .py/.rs/.ts/.tsx excludes vendor/node_modules/dist). 14 test assertions that mirrored translated log/result strings synced to the new English text. Functional CJK data left intact where it is logic, not prose (`.includes('治理')` checks, FORBIDDEN_ZH lists, CJK stopwords as `\uXXXX` escapes, Chinese alias keys). Includes `ruff format` pass + marker re-anchoring after reflow, and merge with #628 (trajectory compile endpoint comments/logs translated, user-facing detail messages marked).

## v0.5.150 (PR #622 — degrade_on_skip graceful degradation)

- **PR #622**: opt-in `NodeConfig.degrade_on_skip` (default OFF, zero behavior change) — when `skip_if` is fully hit because the upstream chain broke (missing outputs), a terminal node with the flag runs anyway on partial inputs instead of skipping itself out of the run. Step tagged `output_data["_degraded"] = <skip_desc>` + summary suffix `(degraded: ...)` so the runs page and downstream condition checks can distinguish partial-input runs. Failed/cancelled/denied paths untouched; permission + HITL gates run normally on the degraded execution (degrade never bypasses approval). Companion to #620; validated in the fusion-horizon long-horizon benchmark (task success 0.375 → 0.875 at ~100 tokens/task). Tests `tests/test_degrade_skip.py` (5 cases).

## v0.5.149 (#617 web state isolation; #618 node repair; #620 condition distillation)

- **#618 (PR #618)**: opt-in `NodeConfig.on_error_repair` (default OFF, zero behavior change) — when a node fails after exhausting its retry budget, the engine makes one LLM call deciding `retry` (re-execute once with LLM-corrected params, re-entering the permission gate), `skip` (mark step SKIPPED and continue), or `abort` (unchanged failure path). Parse/LLM errors fall back to abort (fail-closed); repair is one-shot; decision logged under `[<run_id>] llm_repair:`. Design doc `docs/pr-repair-step.md`; tests `tests/test_repair_step.py` (12 cases).
- **#620 (PR #620)**: condition-extraction layer for trajectory-to-workflow distillation (orchestration flywheel stage 3) — `trajectory/cond_distill.py` with `build_evidence` (ordered node executions → JSON-safe evidence packet), `extract_plan` (one restricted-DSL LLM call compiling `{tool, when}` conditions; model fallback arg > `FUSION_MLX_MODEL` > default), `plan_to_workflow` (linear chain, `when` conditions inverted into native `skip_if` with `negate`); `engine/workflow.py::_skip_if_hit` gains `gte`/`lte` numeric comparisons + `negate` flip (conditions without the new keys evaluate exactly as before). Tests `tests/test_cond_distill.py` (12 cases).
- **#617 (PR #619)**: web state test isolation — `ApprovalJournal` + `RunStateStore` default paths honor `FUSION_COWORK_WEB_DIR` (unset = legacy `~/.fusion-cowork/web`), and a new autouse conftest fixture redirects the web state dir to tmp_path per test. Durable HITL (#463) recovery no longer restores a real machine's stale crashed-run approvals into test clients (observed: 35 leaked approvals). Resolves the audit #617 P2 residual.

## v0.5.148 (issue #614 — office DAG node family)

- **#614 (PR #615)**: new `office` node subpackage — the cowork executor leg of the Agentic Office plan. 4 nodes (`NodeCategory.FUSION_ECOSYSTEM`, registered in `import_all_nodes()`), each a structured office operation against fusion-office office-api (:11492) or the artifacts-engine version chain:
  - `office_spreadsheet` — xlsx validity parse via `POST /v1/xlsx/parse`; invalid workbooks fail visibly (422 → FAILED + error outcome).
  - `office_document` — docx `write` (markdown→docx), `fill` (template + values JSON; `X-Office-Unfilled` leftovers surface in output and map the outcome to `user_edited`), `parse` (docx→blocks).
  - `office_slides` — pptx `parse` and `write` (markdown or ops format, optional base deck for ops mode).
  - `office_artifact` — registers produced content into fusion-artifacts-engine over JSON-RPC (`artifact.create` new chain / `artifact.update` appends a version), `X-API-Key` from `FUSION_ARTIFACTS_API_KEY` when set.
  - Outcome signals (plan §4.1) reported best-effort to office-api `POST /v1/outcomes` with the gateway `rollout_id`: `task_success` / `user_edited` (docx fill leaves unfilled placeholders) / `error`; reporting never fails the node, and no `rollout_id` → no POST.
  - Chinese aliases: 办公文档/文档生成/文档解析、办公表格/表格解析、办公幻灯片/幻灯片生成、成果入库/工件入库.
  - Tests `tests/test_issue_614_office_nodes.py` (17 cases): ASGI stub transports cover all ops + signal mapping + registration; live E2E leg (skips when :11492 down) runs a real xlsx→docx→outcome DAG against the rollout sink; artifact live test gated on `FUSION_ARTIFACTS_API_KEY` (local artifacts-engine fail-closed).

## v0.5.147 (issue #609 — invite token HTTP surface)

- **#609 (PR #611)**: `GenerateInviteNode` (#597) shipped the invite store but no HTTP route could resolve or consume a token — the fusion-site `/invite/[token]` activation page was blocked. New surface:
  - `GET /api/delivery/invites/{token}` — peek (non-consuming): `{token_status, ttl_hours, expires_at, order{order_no,title,stage,repo}}`; 404 `unknown_token` / `token_unavailable` (used/expired); logs at most a 6-char token prefix.
  - `POST /api/delivery/invites/{token}/consume` — atomic single-use consume (`consumed:true`), same shape.
  - `OrderStore.peek_invite(token)` — hash-only lookup joining `invite_tokens` to `orders`; plaintext token never touches storage or logs.
  - `consume_invite` hardened to a single conditional `UPDATE ... WHERE used_at=0` + rowcount check — closes the concurrent double-consume race (schema stores unconsumed as 0, not NULL).
  - Service-key scope (#565) extended to `/api/delivery/invites` so the site BFF can drive activation; still 403 on all other surfaces.
  - Tests: `tests/test_issue_609_invite_surface.py` — 12 cases (peek/consume lifecycle, hashed-at-rest DB check, service-key scope boundaries, log hygiene).

## v0.5.146 (issue #608 — demand-mail-rail scheduled workflow + pipeline run-now)

- **#608 (PR #610)**: the demand mail rail nodes + API (#585-#588/#595) are now wired into a runnable pipeline:
  - `demand-mail-rail` workflow definition (poll_qq_imap -> normalize_lead -> match threshold 3.0 -> dual_draft) seeded **idempotently at app build** — an existing def file wins, operator edits survive restarts.
  - Hourly cron task (`0 * * * *`) created on first serve from a FastAPI startup hook that runs after persisted-task restore, keyed on `workflow_id` — restarts never duplicate; a restored task gets its executor re-bound, lifting the P1-12 auto-pause (user-paused tasks stay paused).
  - `TaskScheduler.run_task_now()` + `POST /api/scheduler/tasks/{id}/run-now` — immediate execution with the same run_count/last_run/fail-threshold bookkeeping as a cron fire; paused/failed → 409, unknown → 404.
  - DemandPage pipeline tab: next/last run times, run/fail counters, Run now + Pause/Resume per task.
  - Acceptance: the rail runs end-to-end with zero LLM calls when no new mail (poll fails fast under `continue_on_error`, rest no-ops on empty store). Tests `tests/test_issue_608_demand_mail_rail.py` (7 cases).
  - Also fixes the pre-existing `test_issue_585` no-credentials case on operator machines where the #595 env file re-seeds process env.

## v0.5.145 (issue #605 — demand direct-ingest endpoint for fusion-site)

- **#605 (PR #606)**: `POST /api/demand/leads` — the direct-ingest channel from the #595 closing contract, unblocking fusion-site#141 (E1: intake form produces a `direct` lead visible in the cowork demand inbox):
  - Body `{source: "direct", external_id, title, description, budget_hint?, skills?, client_meta?}`; `external_id` is the intake flow_id. Dedup on the existing `idx_leads_external(source, external_id)` unique index — duplicate POST returns the existing lead with **200** (`duplicate: true`, stored row never mutated), new lead **201** with `{lead_id, status, matched_capabilities, match_score}`.
  - Same normalize + match stage as mailbox leads: rule spam markers from `classify_rules` (spam filtering stays in the normalize stage), then `match_lead` against the versioned catalog — status `matched`/`parked`/`spam` with `matched_capabilities` + `match_score` stored and echoed. Zero LLM calls in the web plane (`extraction="rules"`), consistent with #595.
  - No network in the request path + catalog seed fallback (same as `GET /api/demand/catalog`) → the fire-and-forget fusion-site caller can never hang.
  - Validations 400: `source != "direct"`, missing `external_id`, empty `title`/`description`; `skills`/`client_meta` sanitized to store limits.
  - Tests `tests/test_issue_605_direct_ingest.py` (6 cases: 201 matched + inbox visibility, 200 dedup without mutation, 400 validation matrix, spam marked not matched, skills/meta persisted, auth required); full suite 3431 tests, 0 failures.

## v0.5.144 (issues #596/#597 — demand operator GUI + factory delivery rail)

- **#596 (PR #602)**: demand operator GUI — new `#/demand` page (`web/src/pages/DemandPage.tsx`) with four tabs wired to the #595 API:
  - **Inbox**: metrics strip + status filter + lead list; dual-draft review with approve A / approve B / edit / discard, and "human sends on the original channel" hints (system never sends).
  - **Catalog**: editable `price_cny`/`enabled` per capability, version auto-bump display, save re-scores parked leads server-side.
  - **Settings**: QQ IMAP config form (authcode write-only, test connection) + read-only model routing card.
  - **Pipeline**: read-only scheduler task/runs view deep-linking `#/runs/<id>`.
  - `web/src/api.ts` demand endpoints + types; manifest `pages` entry; `web/tests/demand596.test.ts` (bun test 166 passing).

- **#597 (PR #603)**: factory delivery rail — demand pipeline (won lead) now flows into delivery with a strict human-gate state machine:
  - **OrderStore** (`fusion_cowork/delivery/store.py`): SQLite, restart-safe, idempotent. ORD-YYYY-NNN orders keyed per lead (one per lead, rerun returns existing). Strict S0-S7 ladder (`created → sow_draft → sow_gate → tad_draft → tad_gate → test_report → acceptance → closed`) — `advance_stage` moves exactly one step; machine nodes write drafts and park on gates; only the web approve route advances out of a gate. Single-use invite tokens (sha256 stored, 72h expiry, plaintext returned once). Issue cache with `sla-24h`/`sla-72h` overdue scan (flagged once). Tamper-evident sha256 audit chain (`audit_verify`) over every gate transition and repo action.
  - **Nodes** (`fusion_cowork/delivery/nodes.py`, 77 → 82 node classes): `create_order` (won lead → order), `scaffold_repo` (private repo per order via gh CLI — token from env only, never logged; "already exists"/422 tolerated so reruns are idempotent), `draft_sow` (deterministic template fill, no re-draft past the gate), `generate_invite` (token + message draft for the human to send — system never sends), `sync_issues` (hourly issue pull + SLA overdue WARNING, source factory/delivery).
  - **Web API**: `GET /api/delivery/orders` (stage filter), `GET /api/delivery/orders/{order_no}` (with `audit_chain_ok`), `POST /api/delivery/orders/{order_no}/approve` — the only human gate-advance path; optional `delivery_db_path` seam.
  - Tests `tests/test_issue_597_delivery_rail.py` (11 cases: state-machine idempotency + no-skip ladder, fake-gh scaffold idempotency + 422 race, GH_TOKEN never in payload/audit, SOW gate parking, invite hash-only/single-use/expiry, SLA one-cycle flag, audit tamper detection). Full suite 3425 tests, 0 failures.

## v0.5.143 (issue #595 — demand engine web API: leads / metrics / catalog / settings)

- **#595 (PR #600)**: `/api/demand/*` surface in `web_api.py` (user-key territory, zero LLM calls — models stay behind gateway :11432; mail content echoed only as `LeadStore` persists it):
  - **Leads**: `GET /api/demand/leads?status=&limit=` (newest first, limit clamped 1..500), `GET /api/demand/leads/{id}` (full record incl. drafts + draft_meta, 404 unknown), `POST /api/demand/leads/{id}/approve` (`{pick: "a"|"b", text?}` — 409 unless `drafted`, 400 bad pick; edited text stored as `draft_final` + `draft_edit_distance` = 1 − difflib ratio vs picked draft), `POST /api/demand/leads/{id}/discard` (optional reason).
  - **Metrics**: `GET /api/demand/metrics` — `LeadStore.metrics()` proxy.
  - **Catalog**: `GET /api/demand/catalog` (runtime file, seed fallback) / `PUT` — validates version + non-empty capabilities, same-version saves auto-bump (`2` → `2.1`), atomic write to the runtime path (never the in-repo seed), re-runs `match_lead` for all parked leads and stamps the new `catalog_version`, returns `{version, previous_version, parked_re_scored}`.
  - **Settings** (QQ IMAP): `GET` (host/port/user/folder, `password_present` flag only, env path, last test result, UID cursor) / `PUT` (password write-only, empty keeps stored; env file written 0600) / `POST /api/demand/settings/test` (read-only EXAMINE connect → ok/latency_ms/cursor, never logs secrets).
  - Support: `demand/store.py` `draft_final` + `discard_reason` columns (schema + `_migrate`); new `demand/settings.py` env-file layer (`~/.fusion-cowork/demand_imap.env`, `FUSION_COWORK_DEMAND_IMAP_ENV` override, process env keeps priority — `ImapConfig.from_env` loads the file); `demand/catalog.py` `runtime_catalog_path()` + atomic `save_catalog()`; `create_web_app` seams `demand_db_path` / `demand_catalog_path`. Tests `tests/test_issue_595_demand_api.py` (14 cases); full suite 3413 tests, 0 failures.

## v0.5.142 (issue #594 — RunGate admission-time reservation closes same-key race)

- **#594 (PR #598)**: a run admitted as `running` did not actually hold its per-key concurrency slot until the spawned `_execute` task reached `gate.acquire()` — between `create_task` and first scheduling, a later same-key run probed the gate unsaturated and was also admitted `running`, violating the per-key cap. Fix: admission-time reservation —
  - `RunGate` (`engine/run_gate.py`): lazy per-key semaphores replaced with plain counters (`_global_running` / `_key_running`); probe+reserve atomic on the single event loop (no await between them). `try_reserve(key)` takes the slot synchronously; `acquire_reserved(key)` polls (0.05s) until a slot frees; `acquire(key)` kept as back-compat entry; `release()` clamps at zero; `status()` keeps the `running_by_key` API.
  - `web_api.runs_start`: reserves the slot at admission (`gate_reserved` on the record); record status is `queued` when reservation fails instead of `running`. `_execute` skips re-acquire for reserved runs; resume/derive paths keep the existing acquire-then-run flow; slot released in the existing `finally`.
  - Tests `tests/test_issue_594_rungate_admission.py` (8 cases: per-key atomicity, global cap, acquire/reserve parity, release clamp, wait-after-release, disabled gate, 5-run same-key burst = exactly one running + rest queued + FIFO drain, slot released on completion); probe node re-registered defensively in the fixture (other test files clear `NodeRegistry` without restore). Full suite 3392 tests, 0 failures.

## v0.5.141 (issues #585-#588 — demand engine M1 mail rail: poll / normalize / match / dual-draft)

- **#585-#588 (PR #589)**: unified Lead pipeline (adapters -> normalize -> match -> draft) as four scheduler-driven nodes over one SQLite store (`fusion_cowork/demand/`):
  - **#585 `poll_qq_imap`**: read-only QQ mail IMAP poll (`BODY.PEEK`, EXAMINE-only select) — per-folder UID cursor in `poll_state` (restart never re-emits), Message-ID dedupe incl. synthetic-id fallback and same-batch/cross-UID duplicates, HTML stripped to plain text, attachments never decoded, password env-only and never in errors.
  - **#586 `normalize_lead`**: raw mail -> unified Lead schema via the local model (fixed prompt + JSON validation) with a deterministic rule fallback on any model failure (node never fails on model-down); platform-id extraction tolerant of punctuation; spam marked, never deleted, excluded from matching.
  - **#587 `match`**: pure-code Lead x capability-catalog scoring (keyword/skill tables + budget-range fit, no LLM per Rule 5); catalog is a versioned JSON file (seed frozen 2026-11-20: RAG 2000 / voice 3500 / extraction 1800 / DAG 2500 / video 4000 / doc 1500 CNY); version bump re-scores the backlog; below-threshold leads parked with best score kept, never dropped; catalog version recorded per row.
  - **#588 `dual_draft`**: draft_a (local gateway model) + draft_b (cloud GLM-5, `FUSION_GLM_API_KEY` optional) per matched lead — fixed proposal template, client brief embedded as JSON-quoted untrusted data with instruction-like lines stripped (prompt injection cannot alter price or policy), catalog-quoted price, no off-platform contact info, per-slot error tolerance, restart-safe re-run, drafts stored only (nothing is ever sent), optional ntfy ping; `LeadStore.metrics()` exposes A/B pick rates, avg edit distance, per-capability win rate.
  - All four registered via `nodes/demand/demand_nodes.py` in `_NODE_MODULES` (73 -> 77 node classes). Tests `tests/test_issue_585..588_*.py` (46 cases: FakeIMAP4SSL protocol stand-in, golden platform fixtures, scoring matrix, E2E draft + injection + metrics).

## v0.5.140 (issue #580 — worktree isolation safety: ownership-proof delete + untrusted-source gate + merge-back)

- **#580 (PR #583)**: the three non-negotiable safety pieces from the paseo analysis design inputs land on the existing `worktree` node / `WorktreeManager`:
  - **Ownership-proof deletion**: `create` writes a `.fusion-worktree.json` marker (name/branch/repo_root/created_at/trust state, atomic tmp+replace); `remove` requires the marker with matching name **and** a `git worktree list --porcelain` cross-check — the old path-tail matching fallback is removed, unowned directories are never deleted (`force` only passes git `--force`).
  - **Untrusted-source gate**: `create(source_repo=...)` marks the worktree `untrusted_source` when the source differs from the repo's own `origin` remote; `exec_in` is blocked until explicitly trusted via the `trust_source` node param or `WorktreeManager.mark_trusted()` (persisted in the marker). Own-origin sources pass through untouched.
  - **Merge-back never occupies the agent worktree**: new `merge_back` action + `WorktreeManager.merge_back()` runs the merge in the base repo's main worktree (never inside the agent worktree), checks out the target branch and restores the original branch in `finally`, refuses a dirty base tree (`git status --porcelain`), and `git merge --abort`s on failure.
  - `worktree` classified HIGH_RISK (arbitrary exec + destructive remove, same exposure class as `shell_exec`). Tests `tests/test_issue_580_worktree_safety.py` (16 cases, real git repos in tmp_path, zero network). Items 4-6 (auto-archive guard, placeholder auto-rename, git priority scheduler) remain design-input in #580.

## v0.5.139 (issue #579 — steer/interrupt directive modes + at-least-once finish notifications)

- **#579 (PR #581)**: run directives gain a dual delivery mode — `mode: "steer"` (default, unchanged: applies at the next node boundary, in-flight attempt completes untouched) vs `mode: "interrupt"` (in-flight node attempts are cancelled and voided at the boundary, an audit marker step with `user_correction=True` is recorded, the directive applies in FIFO admission order alongside any older queued directives, and voided nodes re-run in the next wave with the instruction merged). An interrupt never overtakes an older queued directive — admission-order invariant holds at the interrupt boundary. Interrupt events are consumed at the boundary (engine `_interrupt_events` cleaned up at execute end and after consumption).
- **At-least-once finish notifications**: new `server/push.py` `ReliableEgress` durable outbox — persist-before-ack enqueue (atomic tmp+`os.replace`), `flush_pending()` retries until 2xx and bumps `attempts`; crash between delivery and mark duplicates the delivery (documented at-least-once, not exactly-once). Opt-in via `FUSION_COWORK_PUSH_AT_LEAST_ONCE=1` + `FUSION_COWORK_PUSH_WEBHOOK_URL` (+ optional `FUSION_COWORK_PUSH_OUTBOX_PATH`, default `~/.fusion-cowork/web/push_outbox.json`); `RunRegistry` enqueues `node_finished` on node-end events and `run_finished` on the terminal path, flushes via scheduled task, and recovers pending records at startup (`_recover_runs`).
- **py3.11 cancellation fix (CI shard hang)**: `asyncio.wait_for` on py3.11 swallows `CancelledError` when the inner future completes in the same tick as the outer cancel (`wait_for` returns the result instead of raising) — a run task cancelled exactly as a HITL gate resolved escaped cancellation, parked in a fresh 1800s `_hitl_await`, and pytest-asyncio's `_cancel_all_tasks` gather pended forever. `_hitl_await` now waits on the pause event via a 50ms sleep poll against the deadline (cancellation lands on sleep; `TimeoutError` semantics unchanged), and the kernel `HumanGate.wait` call runs in a cowork-owned task with a poll loop + explicit inner cancel (harness `human_gate.wait` has the same internal `wait_for` swallow). `tests/test_issue_489_rewind_deploy.py` uses the context-manager `TestClient` (un-entered clients open a fresh per-request event loop that cancels background run tasks) and waits for the resumed run to reach terminal state before forcing a running status. Tests `tests/test_issue_579_steer_interrupt.py` (10 cases); full suite green on 3.11/3.12/3.13.

## v0.5.138 (issues #570/#571/#572 — harness sandbox backend + MAST failure attribution + journal hygiene)

- **#570 (PR #574)**: sandbox runner selection routed through harness `executor_backend.select_sandbox_runner` — `FUSION_SANDBOX_BACKEND=fusion_executor` opts impl-node execution into `FusionExecutorBackend` (native sandbox + Git snapshot/rollback); unset or unavailable backend falls back to the current runner (default path zero behavior change). Per-cwd singleton cache (`get_sandbox_runner(cwd)`) so permission-gate policy builds don't create a new backend per tool call and overwrite snapshot state; `reset_sandbox_runner_cache()` for tests/config changes. Engine snapshot before node execution + rollback after final failure when a fusion_executor backend is active (errors logged, never block execution). `build_sandbox_policy()` is now the sole production caller of `kernel_sandbox_policy`. Tests `tests/test_issue_570_sandbox_backend.py` (10 cases).
- **#571 (PR #577)**: MAST failure attribution on the DAG node failure path (fusion-harness `failure_attribution.FailureAnalyzer`), opt-in `FUSION_COWORK_FAILURE_ATTRIBUTION=1` (default off = zero behavior change). On node failure, `attribute_node_failure()` builds kernel records from completed steps + the failing node's error and returns the first verdict (mode/cluster/confidence/evidence); verdict lands on the failed node's `data.attribution`, the execution (`execution.attribution`, first failure wins), and the web run record (live event + terminal result paths). Spec-class failures (cluster `specification_system_design` — unclear goal / ambiguous task / no objective) skip business-failure retries: retry budget not burned, verdict routed to QA/rework instead. Tests `tests/test_issue_571_failure_attribution.py` (7 cases).
- **#572 (PR #573)**: approval-journal ghost rows in `/api/permissions/pending` — rows whose run record is gone entirely are now dropped alongside the existing empty-`run_id` and terminal-run drops (records durable since #399, so absence means the run is unrecoverable and the approval can never be woken or resolved). Tests updated in `tests/test_issue_497_journal_hygiene.py` + `tests/test_issue_463_durable_hitl.py`.
- **CI pin fix (PR #576)**: `tests/test_issue_533_comm_bus.py` kernel `MSG_TYPES` pin updated for fusion-harness 0.29.0 (new `destructive_gate` member, ApprovalGate operator-inbox kind, consumed via #554); `orchestrator/comm.py` module ruling updated to match.

## v0.5.137 (issue #565 — service credential path for storefront bff engine calls)

- **#565 (PR #567)**: dedicated service-to-service key for the fusion-site storefront bff calling the engine web API — `FUSION_COWORK_SERVICE_KEY` (both sides' env) accepted alongside the static env key + gateway-issued `fm-` module key (`check_static_key` key_id `"service"`, permissions `["runs"]`, counts toward `auth_configured()`); scope enforced in `_auth_middleware` — service key valid only on `/api/runs*` (create/poll/resume-from/cancel/directive), any other `/api/*` path returns `403 {"error":{"code":"forbidden"}}`; user keys unchanged (full reach), `/health` stays open. New `docs/engine-service-contract.md` — credential provisioning, endpoint map (POST /api/runs with seeded `workflow_id` e.g. `product_lifecycle_agents` + `inputs`, GET poll with per-step evidence, resume-from body `{"node_id": ...}`), minimal httpx bff flow, failure semantics (401/403/HITL pause/crashed-run recovery). `docs/api-reference.md` resume-from body corrected `step_id` → `node_id`. Tests `tests/test_issue_565_service_key.py` (13 cases: key ids, absent env, auth_configured, run create/poll 202/200, out-of-scope 403 matrix, resume-from route in scope, invalid 401, user-key no-regression, /health open).

## v0.5.136 (issue #563 — kernel vocabulary wiring: HITL marker re-delivery + model_tier + fan-out bounds)

- **#563 (PR #564)**: companion adoption to #558 against fusion-harness 0.27.0 (#141/#142/#145 tier vocabulary + #125/#124 constants):
  - **HITL restart re-delivery (#125 companion)**: after a SIGKILL mid-HITL-wait the kernel HumanGate queue keeps the pending question on disk; new `WorkflowEngine.hitl_recover(exec_id)` surfaces it read-only and verifies the `awaiting_human` checkpoint marker against the kernel CheckpointStore (when wired). The resume path (`_hitl_await`) rebinds the original pending question for the same node instead of asking a duplicate — resolve against the original wakes the resumed run (single-winner semantics preserved). `runs_get` exposes `hitl_recover` on running records whose engine memory is empty after a crash.
  - **model_tier vocabulary (#141)**: `workflow_ladder` workers accept an optional `model_tier` hint validated client-side against kernel `VALID_MODEL_TIERS` (''/cheap/strong) for every stage UPFRONT before any dispatch — invalid tier = typed error, no partial fan-out; valid tiers pass through `SubagentSpec.metadata` and ride the stage report.
  - **node-level fan-out bounds (#124)**: `workflow_ladder` pre-validates per-stage worker counts before dispatch — above `FANOUT_HARD_MAX` (20) the node fails with a typed error; above `FANOUT_COMPLEX_MAX` (15) the kernel `TOKEN_MULTIPLIER_WARNING` rides the stage report (`warnings` field, kernel wording).
  - Pin suite `tests/test_issue_563_kernel_vocab.py` (13 cases).

## v0.5.135 (issue #558 — P-series knobs complete: phase evaluator + handoff attribution + duplicate work)

- **#558 (PR #561)**: items 2-4 shipped with fusion-harness 0.26.0 on PyPI (harness#140 closed); slice 1 (items 1+5) landed in v0.5.134. All opt-in, defaults = current behavior:
  - **#133 phase evaluator**: `workflow_ladder` gains `phase_evaluator` param (default false) — deterministic checks between member phases (empty results / all-failed phase) block the phase via kernel `ParallelTeam` (survivors marked "blocked", `team_evaluator_block` emitted); stage reports gain `blocked` counter; blocked members never feed ladder propagation, so error compounding between stages stops.
  - **#132 handoff attribution**: terminal failed runs attach `failure_attribution` (`handoff_candidates` A→B edges / `decisive_moment` / `responsible_agent`) via kernel `FailureAnalyzer` deterministic keyword heuristics — no model call, no replay; only failed steps carry "error" events so the repeated-tool-call heuristic stays silent on normal DAG steps. RunViewPage renders the candidates.
  - **#134 duplicate work**: `FUSION_COWORK_TEAM_DUPLICATE_THRESHOLD` (int ≥ 2, default off) wires `TeamRuntime` duplicate detection in `SpaceKernelPlane`; `team_duplicate_work` events captured per space via minimal bus adapter (bounded 100/space) + warning logs.
  - Pin suite `tests/test_issue_558_p_series.py` grown to 22 cases.

## v0.5.134 (issue #558 — P-series kernel knobs, slice 1: durable HITL + fanout)

- **#558 (PR #559)**: adopts fusion-harness P-series knobs items 1+5 (0.25.0-capable); items 2-4 (phase_evaluator / handoff attribution / duplicate_threshold) wait on harness 0.26.0 PyPI release (dahai80/fusion-harness#140):
  - **#125 durable HITL pauses**: `WorkflowEngine` builds its lazy kernel `HumanGate` with `checkpoint_ref=KernelCheckpointBinder.store` when kernel checkpoints are wired — pending HITL questions mirror as `awaiting_human` checkpoint entries (session = run id) and update on resolve, so approvals survive process restarts and can be re-delivered after a DAG restart. Unwired binder = plain gate (JSON queue only), zero behavior change. New `KernelCheckpointBinder.store` accessor; gate construction deduped into `_build_kernel_gate` (shared by `_hitl_gate_ask` + `_ensure_kernel_gate`).
  - **#124 fan-out ladder**: `SpaceKernelPlane` evaluates roster size against kernel `FANOUT_*` bands at runtime spawn — non-fatal warnings logged + surfaced per space on `plane._fanout_warnings`, cleared when the roster returns in-band.
  - Housekeeping: chaos s3 test falls back to `fusion_harness.policies` for `Deadline`/`RetryPolicy` (demoted from harness root exports in 1c779ef).
  - Pin suite `tests/test_issue_558_p_series.py` (9 cases).

## v0.5.133 (issue #554 — kernel destructive_gate + artifact_scan + json_repair wiring)

- **#554 (PR #556)**: adoption-audit mechanisms from fusion-harness v0.20–0.22 now wired into cowork node execution and load paths, via thin adapter `engine/kernel_gates.py` (decision semantics stay in the kernel; cowork keeps consumer wiring):
  - **destructive_gate**: `ApprovalGate.classify` (kernel table + name tokens delete/remove/drop/purge/...) runs in `PermissionManager.check` for non-HIGH-RISK names — destructive classification routes into the existing `_check_high_risk_guard` HITL park; approval mints a single-redemption kernel token (`apt_...`, idempotent issue, replay rejected) recorded in run state. Zero behavior change by default: every existing node the kernel classifies destructive is already in `HIGH_RISK_NODES`/`HITL_GATE_TOOLS` (asserted by test). `KernelPermissionBridge` forwards explicit cowork deny rules as kernel `permission_engine` denies before the human gate.
  - **sandbox fail-closed**: `FUSION_COWORK_SANDBOX_TOOLS` (comma-separated node names) maps tools to kernel `ExecutionTier.SANDBOX`; with no runner wired (`FUSION_COWORK_SANDBOX_RUNNER=1` opts in to the reference macOS `sandbox-exec` runner) the check denies before approve rules — approvals cannot bypass. Unset = no tier mapping, zero behavior change. Explicit env mapping only — risk-score auto-tier was rejected to keep the #73 guard path intact.
  - **artifact_scan**: skill-pack load (`skills/persistence.list_skill_packs`), memory-retrieve blocks (`MemoryRetrieveNode`), and approval-journal decision replay scan text through kernel `scan_artifact` (prompt-injection/credential-leak rules); hits are quarantined (`ArtifactQuarantine`, path `FUSION_COWORK_ARTIFACT_QUARANTINE`, default `~/.fusion-cowork/web/artifact_quarantine.json`) and skipped/fail-closed rather than loaded or replayed.
  - **json_repair**: LLM output parsing in AI nodes (classify/summarize/rename) and `NLWorkflowGenerator` routes through `parse_llm_json` — strict parse first, then the kernel deterministic repair ladder (fences/balanced extract/quotes/trailing commas/truncated close); unrepaired input raises typed `JSONRepairError` (existing fallbacks unchanged). Repair count is tracked per node step (contextvar bucket) and emitted in `dag_node_finished` telemetry payload + run-API step dict (`json_repairs`).
  - Pin suite `tests/test_issue_554_kernel_wiring.py` (25 cases): classification + zero-new-gate invariant, park→approve token grant / deny→FAILED, sandbox fail-closed with approval, bridge deny-before-human, poisoned skill pack + journal replay isolation, repair ladder + telemetry count.

## v0.5.132 (issue #545 — DAG run/node telemetry, flywheel Loop D foundation)

- **#545 (PR #552)**: workflow engine emits DAG execution telemetry into `fusion_ops.ops_events` via `fusion_core.ops.OpsEmitter` — `dag_run_started`/`dag_run_finished` (outcome, total `latency_ms`, definition hash) + `dag_node_started`/`dag_node_finished` (node type, `duration_ms`, attempt count). Stable failure signature `node_type:ErrorClass` (no message text) = mined_issues cluster key; repeated failing nodes group across runs. Node inputs/outputs never emitted (tenant data red line); only sha256 of canonical definition (node types + edge topology, instance-id free). Opt-in `FUSION_OPS_DSN` (unset = zero behavior change); emit never blocks (drop + counter). Pin suite `tests/test_issue_545_ops_telemetry.py` (19 cases).

## v0.5.131 (issues #549/#544 — runs API HITL marker hygiene + IDOR regression matrix)

- **#549 (PR #550)**: `runs_get` mutated the shared run-registry record dict on mid-run GETs (`hitl_pending`/`hitl_deadline`/`awaiting_human`/`awaiting_node`); those keys survived into terminal responses so a successful run kept reporting `hitl_pending: true`. Terminal responses now pop all four markers. Pin suite `tests/test_issue_549_stale_hitl.py`.
- **#544 (PR #550)**: IDOR regression matrix for the module api-key chain — `tests/test_issue_544_idor_matrix.py` covers workflows/runs/artifacts/run events/scheduler tasks x foreign api-key / foreign user JWT / no auth / revoked api-key; every cell must be 401/403/404, never 2xx with foreign data. Auth stays centralized in `_auth_middleware` (single module key = admin, no per-endpoint bypass surface).

## v0.5.130 (issue #527 — HITL approval gate on the shared kernel HumanGate contract)

- **Kernel queue wiring (#527, PR #547)**: the workflow-engine HITL park point now asks a `fusion_harness.HumanGate` question — `ask(session_id=run_id, policy="block", ttl_s=<engine deadline>, requester="workflow_engine", payload=sanitized gate input)` — and the parked loop blocks on kernel `wait()`; the approval queue is the shared kernel contract, not a cowork-local mechanism
- **Single-winner approver side**: `resolve_hitl` routes through kernel `resolve()` — concurrent/duplicate confirms get `resolved=False` (reason answered/cancelled/expired) instead of silently overwriting the recorded decision; out-of-band kernel resolves are adopted by the engine (approved/once) with the #453 pause-event invariant preserved
- **ttl expiry replaces ad-hoc pending accumulation**: `ttl_s` carries the deadline into the kernel queue (lazy expiry); engine timeout auto-deny, `hitl_timeout` hook and `COMPLETED_WITH_DENIALS` terminal semantics unchanged (#187)
- **Approval-routing policy stays cowork**: guard verdicts, HITL_GATE_TOOLS one-shot gate decisions, approval journal replay (#463/#507), once-grants, param overrides, batch verdicts, revision loop — untouched; kernel contract is queue semantics only, as specified in #527
- **W8 bridges**: engine resume/cancel cancel the pending kernel question so the parked wait wakes and re-asks without synthesizing a decision (#445 semantics)
- **Seam**: `WorkflowEngine(hitl_gate=...)` injection + `FUSION_COWORK_HITL_GATE_DIR` (default `~/.fusion-cowork/hitl_gate`; test conftest pins the suite to a tmp dir); verified by `tests/test_issue_527_hitl_humangate.py` (8 cases) plus the full HITL regression batch

## v0.5.129 (issue #533 — K4 Phase B: orchestrator kernel convergence)

- #533 item 1 (#539): `orchestrator/comm.py` MessageBus ruled **kernel-verified DIVERGENT-BY-DESIGN** — kernel mailbox is member-scoped (closed `MSG_TYPES`, durable state_dir persistence); cowork bus stays the open in-process pub/sub transport (arbitrary topics/payloads, `*` broadcast, E-6 `MessageDeliveryError` + dropped audit). Pin suite `tests/test_issue_533_comm_bus.py`.
- #533 item 2 (#540): `orchestrator/role_registry.py` — `BusinessRole` composes a kernel `TeamMember`; `role_id`/`executor`/`duties` become derived properties (single member-spec representation); registry stays authoritative for cowork-only `acceptor`/`deliverables`. Pin suite `tests/test_issue_533_role_registry.py`.
- #533 item 3 (#541): `orchestrator/runner_pool.py` — the #494 Part B `run_member` divergence explicitly re-ratified against the kernel roadmap 裁决: kernel `run_member` = LLM-subagent mechanism (space plane #496, `teams.run_member` node); RunnerPool = workflow dispatch with durable at-least-once mailbox semantics. Pin suite `tests/test_issue_533_runner_pool.py`.
- #533 item 4 (#542): task lifecycle converged onto the kernel `TeamTask` state machine — new `orchestrator/task_lifecycle.py` derives `TASK_TRANSITIONS` from the kernel table (+ documented failed/skipped/rework extensions), all AgentTask status writes guarded (`TaskTransitionError` on illegal), kernel-style compare-and-set `claim_task` wired into both execution entries. Fixes silent re-execution of the completed planner task across `execute_plan` re-invocations. Pin suite `tests/test_issue_533_task_lifecycle.py`.
- #533 item 5: `orchestrator/trajectory_writer.py` verified unchanged (kernel trajectory schema wiring from #520 intact).
- Hard gates held throughout: audit suites (`test_audit_fixes_0912.py`, `test_stage6_medium_low.py`, `test_concurrency_stage4.py`) green unmodified; no web_api/desk_rpc/cli behavior change.

## v0.5.128 (issue #524 — kernel SandboxRunner/CodeExecutor conformance)

- **K4 substrate conformance (#524, PR #536)**: cowork executors now satisfy the fusion-harness sandbox contracts — `PythonREPLNode` and `ShellExecNode` implement the kernel `SandboxRunner` protocol at the PROCESS tier (`prepare()` / `run()` / `teardown()` lifecycle, structured result), and `PluginSandbox` implements the SANDBOX tier via the shared helpers in `fusion_cowork/security/kernel_sandbox.py`
- **Kernel CodeExecutor**: new `PythonREPLCodeExecutor` implements the kernel `CodeExecutor` protocol — synchronous subprocess execution with result-variable round-trip and stdout capture, so the kernel can execute Python through cowork's REPL contract
- **No behavior change**: existing node execution paths, permission gating and HIGH_RISK classification untouched; the conformance layer is additive and verified by 41 new protocol conformance tests (`tests/test_issue_524_kernel_sandbox_conformance.py`)

## v0.5.127 (issue #523 — K4 :11442 product-level gate)

- **K4 Phase A closeout (#523, PR #534)**: the roadmap's "grep `:11442` only product call sites remain" exit criterion is now a permanent CI gate — `tests/test_issue_523_product_level_11442.py` fails on any new `:11442` reference in `fusion_cowork/` unless the file is classified in `_CLASSIFIED_SITES` with a product-level rationale
- **Classification durability**: each allowlisted site must keep its `# product-level call (roadmap §6)` in-code annotation, and pruned sites must be removed from the allowlist — the list mirrors reality, the 2026-11-18 audit's raw-grep false positives cannot recur
- **Test-only change**: no product code touched; the 5 re-flagged sites verified product-level per the #494 Part C classification (agent-studio-side entities with no in-process kernel counterpart). Phase B (orchestrator kernel convergence) tracked in #533

## v0.5.126 (issue #522 — budget funnel duck-type)

- **BudgetTracker duck-type (#522, PR #531)**: `BudgetTracker` now satisfies the kernel funnel budget duck-type — public `is_exceeded(graph_id="")` (enforce-aware) and `spent_tokens` attribute matching `funnels._budget_exceeded` / budget-stage `getattr` consumers; USD cost dimension kept additive
- **Private reach-through removed**: the workflow HI-19 budget check uses the public `is_exceeded()` / `record` / `max_budget_usd` accessors instead of `_record.cost_usd` / `_enforce` / `_max_budget_usd`; behavior unchanged
- **ApprovalJournal store_root (#531 drive-by)**: the journal DB now honors `store_root` (`<store_root>/approval_journal.db`, same pattern as `run_states.db`) instead of always defaulting to the global `~/.fusion-cowork/web/approval_journal.db`; prod path unchanged, stale-row leakage into isolated tests fixed
- **CI flake fix**: HITL email opt-in test replaces fixed sleeps with a poll loop (daemon-thread send timing under CI load on py3.13 shards)

## v0.5.125 (issue #520 — kernel trajectory schema)

- **K3 closeout (#520, PR #528)**: cowork trajectory writers emit the kernel `fusion_harness.trajectory` TrajectoryRecord schema — the local `TrajectoryEvent` dataclass is removed, `fusion_cowork` now contains zero local trajectory schema; `~/.fusion/trajectories/cowork/` destination, rotation and retention behavior unchanged
- **Semantic event preservation**: every cowork event (14 HookEvent values + directive / plan_retrospective / task_step / task_acceptance) maps to the nearest kernel normative event with the semantic name kept in `payload.legacy_event`, mirroring the kernel reader's legacy convention
- **Graceful degradation**: bare installs without the [plugins] extra disable trajectory recording with a one-time actionable WARNING instead of writing a non-kernel schema (issue #196 precedent)
- **Reader back-compat**: plan retrospective / task step readers accept both kernel-shaped lines (ts ms) and pre-#520 legacy lines (ts seconds), ts normalized to seconds at the boundary so `after_ts` watermarks keep their unit
- **Round trip verified**: cowork-written trajectories load via kernel `TrajectoryReader` (`test_kernel_roundtrip`); all trajectory tests migrated, full suite green

## v0.5.124 (issue #517 — decision_gate node)

- **Typed-decision lane (#517, PR #525)**: new ecosystem node `decision_gate` (aliases 决策门/智能决策) — short-state gating decisions (escalate-or-continue, retry-vs-fail, route selection) call the fusion-mlx decision head instead of burning a full LLM turn; `POST /v1/decisions` ({state, questions}) or `POST /v1/laya/decide` ({prompt, preset}), URL default fusion-mlx :11434 with `FUSION_MLX_DECISION_URL` override
- **Conservative fallback**: `noul` / confidence below threshold / timeout / transport errors fall back to a single LLM turn (model param or `FUSION_MLX_MODEL`); `fallback_llm=false` or missing model fail visibly instead of guessing
- **Self-heal auth**: stale legacy env key 401s trigger one retry with the settings.json backend key (issue #204 two-key model)
- **Short-context constraint**: decision state compacted and hard-truncated to 900 chars (decision head max_len 1024)
- **Savings ledger**: `decision_meta` records engine, route, latency_ms, fallback_reason, usage tokens and `tokens_saved_vs_llm_turn` (laya lane burns zero generation tokens; live P50 ~9ms warm vs multi-second LLM baseline)
- Tests: `tests/test_issue_517_decision_gate.py` (14 cases: both routes, state truncation, all fallback triggers, visible failure modes, fenced-JSON LLM parse, alias/registry, DAG integration)

## v0.5.123 (issue #516 — workflow asset discipline)

- **Versioned workflow assets (#516, PR #518)**: every `POST/PUT /api/workflows` save produces an immutable version (v1, v2, ...) with a canonical sha256 digest via the new `WorkflowAssetStore` (`fusion_cowork/server/workflow_assets.py`); a gate-rejected candidate never becomes the current definition (previous active version stays current), so a modified workflow without a passing gate cannot become active
- **Machine-checkable acceptance gates**: `node_count` / `required_nodes` / `forbidden_nodes` / `required_params` rules evaluated at activation; unknown rule types fail closed; gate failure returns `409 asset_gate_rejected` with per-rule results
- **Regression gate (opt-in `regression.enabled`)**: the candidate definition replays in an isolated thread against recorded golden executions (per-node output digests); mismatch, missing golden cases, replay error or timeout blocks activation
- **Independent audit trail**: per-asset `audit.jsonl` records actor (masked caller key), timestamp, version, digest, prev digest, node-level diff and gate results — queryable via `GET /api/workflows/{id}/audit` without reading execution logs
- **Run↔asset binding**: runs started by `workflow_id` record `workflow_version` + `workflow_digest`; successful runs are recorded as golden regression cases (`RunRegistry.asset_recorder` seam, mirroring `capability_recorder`)
- **Query routes**: `GET /api/workflows/{id}/versions`, `/versions/diff?v1=&v2=`, `/versions/{n}`, `/audit`
- Tests: `tests/test_issue_516_workflow_assets.py` (17 cases: digest stability, all four acceptance rule types + fail-closed, version activation/rejection, audit diff, regression accept/reject/replay-error, golden suite cap, web route flows incl. run version stamping)

## v0.5.122 (issue #508 — approval checkpoint extensions)

- **Structured human reply (#508, PR #514)**: `resolve_hitl(..., reply={...})` stashes the payload on approve; the engine merges it into the gated node input as `human_reply` (same seam as `edited_params` #225 / `batch_verdict` #309) and `approval_gate` passes it through to its output data so downstream nodes read the human's answer; web confirm route accepts `reply` on both the once-approve and confirm paths (TypeError fallback chains kept)
- **Per-node timeout policy**: `approval_gate.timeout_policy.timeout_s` overrides the engine-global `FUSION_COWORK_HITL_TIMEOUT` deadline; `on_timeout: "deny"` synthesizes a deny decision routed through the common deny path (revision loop registers, journal settled + consumed), default stays fail-fast terminal; the W8 pause event — borrowed and cleared by the HITL wait — is restored on the deny path, otherwise the revision loop hangs at the W8 checkpoint
- **Awaiting branch state**: `engine.hitl_waiting_node(run_id)` exposes which node waits on a human decision; run detail exposes `awaiting_human`/`awaiting_node`
- Tests: `tests/test_issue_508_approval_checkpoint.py` (10: reply flow into gate input/output/downstream, reply ignored on deny, timeout override, timeout-deny revision re-ask, malformed policy fallback, awaiting state + run detail + web reply wiring, gate unit passthrough + schema)

## v0.5.121 (issue #509 — NL generator strips hallucinated file paths)

- **Deterministic path sanitize (#509, PR #512)**: the NL delegation generator could invent local paths the user never gave (e.g. `~/Documents/支出记录`), so the first run always failed on a nonexistent path. Post-parse sanitize now blanks any path-like param value (`~/`, `/`, `./`, `../` prefixed; `scheme://` URLs excepted) that does not appear in the user prompt or context, records it in `workflow_def["warnings"]`, and logs the removal; user/context-provided paths pass through untouched. `SYSTEM_PROMPT` forbids inventing paths (sanitize does not rely on prompt compliance)
- **Preview surface**: `chat-delegate` plan carries `warnings`; DelegatePage renders an amber warning banner so blanked paths are visible and fillable before approval
- Tests: `tests/test_issue_509_nl_path_hallucination.py` (10: blank+warning, user/context path kept, URL untouched, dot-relative checked, non-path params untouched, generator e2e via mocked mlx client, prompt rule)

## v0.5.120 (issue #507 — journal HITL decisions consumed on use)

- **Regression fix (#507, PR #510)**: nightly-20261117 flagged cowork gate/revision tasks looping to `error_max_turns` (c12/c22/c23; degraded streak since 11-14). Root cause: the #463 durable replay (`ApprovalJournal.decision_for`, keyed `run_id`+`node_id`) re-applied the first deny when the #328 revision loop re-evaluated the same gate node — second ask auto-denied without waiting, run failed, fresh pending row dangled. Regression window matches #463 (v0.5.111, after the 11-13 floor runs)
- **Fix**: journal decisions are consumed on use — new `ApprovalJournal.consume_run_node(run_id, node_id)` flips decided rows to `status='consumed'` (kept for audit, excluded from `decision_for`); `_hitl_await` consumes in all three adoption paths (durable replay, normal `resolve_hitl` decision, timeout auto-deny). Crash-restart replay contract kept: undecided-but-recorded decisions still replay exactly once
- Tests: `tests/test_issue_507_journal_replay_revision.py` (4: revision loop survives a wired journal, normal/timeout decisions consumed, restart replay applies once then spent); `tests/test_issue_463_durable_hitl.py` updated to the consumed-on-use contract

## v0.5.119 (issue #496 — space chat on kernel TeamMessage plane)

- **Message plane re-point (#496, PR #505)**: `space/kernel_plane.py` (new) — per-space in-process kernel `TeamRuntime` built from the space roster (members + agents); choice documented: the studio daemon `teams.*` surface is a task-board dispatcher with no chat send/inbox plane, so in-process runtime matches the topology. Kernel provides member-validated send, @mention validation, disk-write-confirmed delivery, `reply_to` threading, per-member inbox files verbatim; cowork maps roster ids to kernel member names (deterministic hash) and carries SpaceMessage metadata in the record `transcript` envelope
- **Write path retirement**: `send_message`/`stream_message`/`_agent_respond`/`relay_agents` all write through the plane (mention routes to target member inbox, broadcast fan-out shares one msg_id, ref-only attachments — non-strings dropped+logged); `list_messages` dual-reads kernel plane + legacy chat-table rows (roadmap §7.3 transition); `/api/sessions` space summary `last_message` reads the plane with chat-table fallback; single-member space delivers to self (kernel permits sender==recipient)
- **Web surface**: vendored `@fusion/chat` (fusion-web-shell#157, byte-identical) + `web/src/spaceChatAdapter.ts` (ChatAdapter over the space REST surface, 3s poll subscribe) + SpacePage messages tab renders `ChatView` (mentions/replies capability-gated); `sendSpaceMessage` passes `parent_msg_id`; `GET /spaces/{id}/agents` getter added
- Tests: `tests/test_issue_496_space_kernel_plane.py` (9: mention→kernel inbox not store, unknown mention fails, broadcast single msg_id, reply threading, ref-only attachments + artifact read, legacy dual-read, rebuild persistence, roster APIs intact) + `web/tests/spaceChatAdapter.test.ts` (3) + model-selector stub roster update

## v0.5.118 (issue #497 — journal pending rows hygiene)

- **Approvals inbox hygiene (#497, PR #503)**: `/api/permissions/pending` journal merge no longer surfaces unresolvable `approval_journal` rows — empty `run_id` (recorded before the run binding, can never be woken/resolved) and rows whose run reached a terminal state are skipped; active-run and unknown-run (pre-#399 history) rows stay visible (conservative). Root cause: nightly eval verify filters pendings by node_id/tool_name across ALL runs — one stale row from an old nightly failed every later verification. Dropped-row count logged INFO (issue #497)
- Tests: `tests/test_issue_497_journal_hygiene.py` (4 cases: empty-run_id dropped, terminal-run dropped, active-run kept with source=journal, unknown-run kept)

## v0.5.117 (issue #494 — node substrate + orchestrator kernel convergence, parts A/B/C)

- **Part A node substrate (PR #500)**: `engine/node.py` BaseNode subclasses kernel `fusion_harness.dag_node.BaseNode` (id generation + `node_type` stamping shared); `NodeRegistry` routes registration through a kernel `NodeRegistry` instance — dup-reject (`existing is not node_class`) is kernel code, cowork mirror dict kept for alias/unregister/clear (rebuild + stale-rebuild retry on external `_registry` mutation); coercion stays local lenient per-field (kernel `coerce_params` strict all-or-nothing breaks LLM-generated params — parity gap filed upstream fusion-harness#39); `checkpoint_binding.NodeStateBinder` divergence documented (session keying `run:node` vs run_id); recovery replay test: mid-run crash on 3-node chain, same `execution_id` re-dispatch replays committed receipt without re-execution
- **Part B orchestrator boundary (PR #501)**: `BusinessRole.to_team_member` + `RoleRegistry.to_team_definition` export the business-role catalog as kernel `TeamMember`/`TeamDefinition`; `row_to_team_message` exports mailbox rows as kernel `TeamMessage`; mailbox storage stays the cross-repo SQLite protocol with agent-studio (kernel MSG_TYPES closed set + member-only senders documented as divergence); runner_pool workflow-dispatch divergence documented; persisted-state compat tests (mailbox/roster state reloadable by fresh instances)
- **Part C :11442 audit (PR #501)**: all 8 hardcoded agent-studio endpoints annotated `# product-level call (roadmap §6)` — every site drives agent-studio-side entities (graph/agent runs, team members, approvals store, capability catalog); grep exit criterion met
- Tests: `tests/test_issue_494_part_a_node_substrate.py` (9) + `tests/test_issue_494_part_b_team_convergence.py` (8)

## v0.5.116 (issue #493 — kernel scoped-permissions migration boundary)

- **Scope-matching delegation (#493, PR #498)**: `Permission.matches` file:/command:/url: prefix matching now delegates to `fusion_harness.scoped_permissions._scope_match` (kernel path-boundary fix adopted: `file:/data` no longer matches `/database`; same fix applied to cowork path keys `path`/`output_path`/`save_path`/`source_path` with expanduser on both scope and value); wildcard/subject/empty-params semantics preserved exactly (regression net unchanged)
- **Deliberate non-migrations documented**: `engine.check()` stays cowork-authoritative (kernel ScopedPermissionEngine folds BYPASS/allow-rule/default-allow into one "allow" — tri-state lost, breaks CR-16 ordering); pending-approval queue stays local (kernel PendingApproval lacks card context; kernel resolve() auto-appends allow rules — conflicts #340 confirm-does-not-persist); hook/HITL/trusted/capability/guard = consumer wiring per kernel contract
- **Shim**: `engine/permission.py` re-exports kernel `ScopedPermissionEngine`/`ScopedPermission`/`PendingApproval`/`ScopedPermissionError` + DECISIONS as `KERNEL_DECISIONS`
- Tests: `tests/test_issue_493_kernel_permissions.py` (16 cases: scope-matching parity, on-disk permissions.json compat, consumer-wiring invariants, shim re-exports)

## v0.5.115 (issue #489 — pipeline stage rewind + deployment executor)

- **Rewind API (#489, PR #492)**: `POST /api/runs/{run_id}/rewind` `{node_id, rule?, actor?}` — gate rejection semantics: re-executes from the rewind point onward, steps consumed before it are snapshotted into the resumed run (paid model work never re-executed, resume_from chain mechanics with ancestor summary + token budget); idempotent per run (same `(run_id, node_id)` key returns the same active resumed run); every rewind recorded on the original run (`rewinds: at/node_id/rule/actor/new_run_id`) and mirrored into the resumed run labels (`rewind_of`/`rewind_rule`/`rewind_actor`); gate→node mapping stays caller-encoded
- **deploy_service node (ops, #489)**: deployment executor with healthcheck and app endpoint — `target=docker` (pull → replace container → run -d with env/ports), `target=launchd` (plist validation → launchctl bootstrap, load fallback), `target=script` (install script); stage chain `pull → start → healthcheck` with per-stage status/attempts/duration/log excerpt + stage retries; healthcheck polls until HTTP 200 within deadline; endpoint returned only when healthy (no fake preview); output `{deployed, endpoint, stages, summary}` consumable by a site deployment GUI via run steps
- **Registration**: HIGH_RISK_NODES (local command execution + service lifecycle), aliases 应用部署/部署服务
- Tests: `tests/test_issue_489_rewind_deploy.py` (13 cases, zero network / zero subprocess in CI)

## v0.5.114 (issue #488 — team_call DAG node)

- **team_call node (#488, PR #490)**: drive agent-studio team members from cowork workflows — `nodes/ecosystem/team_node.py::TeamCallNode` calls the `teams.*` dispatcher over JSON-RPC 2.0 (`POST /v1/rpc`, `x-api-key` auth, same seam/env as `studio_run`); `mode=run` (default) = `teams.add_task` → `teams.run_member` → poll `teams.status` to task terminal (output: task_id / status / summary / artifact_ref); `mode=add_task` = board write only (one-way DAG → board mirror with `blocked_by` + `artifact_refs` per design doc §5.4); `mode=run_member` = run an existing task_id
- **Lifecycle guard**: inactive team state (paused/disbanded) rejects before any board mutation and again mid-run (task preserved, never mutated by the DAG side); RPC failure leaves the board untouched; timeout detaches with task_id kept on the board (studio_run detach semantics); T3 missing (JSON-RPC -32601) fails loud with a dispatcher hint — no silent degradation
- **Registration**: HIGH_RISK_NODES (remote multi-step agent execution), aliases 团队调用/团队任务
- Tests: `tests/test_issue_488_team_call.py` (17 cases, JSON-RPC MockTransport stub)

## v0.5.113 (issue #485 — templates as workflow definitions: seed, install, per-run params)

- **Template seeding (#485, PR #486)**: `_seed_template_workflows` registers every builtin (+ custom-dir) template with a workflow graph into the web `WorkflowDefStore` at `create_web_app` startup — fresh deploys can kick off runs by workflow_id immediately (fusion-site factory kickoff no longer 404s `workflow_not_found`). Idempotent via a digest over the raw template workflow (assembled dicts embed timestamps and never converge); refreshes only its own `template_seed` records when the template changes; never clobbers operator-created or install-source definitions
- **Site contract alias**: `product_lifecycle` template declares `web_aliases: ["product_lifecycle_agents"]` — the id fusion-site factory kicks off is registered as an alias record alongside the primary
- **`POST /api/templates/{id}/install` (#485)**: on-demand registration of any template as a workflow definition (custom templates published after startup, explicit installs) with param assembly, alias ids installed alongside, 400 `params_missing` on unresolved template params
- **Per-run params on `/api/runs` (#485)**: template-backed records (`template_id` present) accept `params` — the source template is re-assembled with caller `param_values` before the run starts; `params` on non-template records → 400
- **Fix**: restore the `hitl_notifier` guard a merge artifact swallowed into a comment line in `create_web_app` (injected notifiers are no longer overridden)
- Tests: `tests/test_issue_485_template_seed.py` (13 cases)

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
