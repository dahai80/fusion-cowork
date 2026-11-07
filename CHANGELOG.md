---
doc_id: fusion-cowork/changelog
product: fusion-cowork
class: platform
entry: service
doc_type: changelog
title: {zh: "变更日志", en: "Changelog"}
audience: engineer
status: draft
version: 0.5.85
updated: 2026-11-07
owner: @dahai
source_of_truth:
  - CHANGELOG.md
  - pyproject.toml
---

# Changelog

Entries are distilled from git tags and merge-commit subjects; full per-issue notes live in the referenced issues and the git history. Format: Keep a Changelog, versions are git tags.

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
