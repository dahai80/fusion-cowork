---
doc_id: fusion-cowork/changelog
product: fusion-cowork
class: platform
entry: service
doc_type: changelog
title: {zh: "变更日志", en: "Changelog"}
audience: engineer
status: draft
version: 0.5.93
updated: 2026-11-08
owner: "@dahai"
source_of_truth:
  - CHANGELOG.md
  - pyproject.toml
---

# Changelog

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
