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
