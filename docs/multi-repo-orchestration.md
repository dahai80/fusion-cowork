---
doc_id: fusion-cowork/multi-repo-orchestration
product: fusion-cowork
class: platform
entry: developer
doc_type: architecture
title: { zh: "多仓库工作流编排规格 (issue #653)", en: "Multi-Repo Workflow Orchestration Spec (issue #653)" }
audience: engineer
status: draft
version: 0.1.0
updated: 2026-11-29
owner: "@dahai80"
source_of_truth:
  - docs/multi-repo-orchestration.md
---

# Multi-Repo Workflow Orchestration Spec (issue #653)

Status: draft v0.1 (2026-11-29). Spec-first — runtime lands behind `FUSION_COWORK_MULTI_REPO=1` (default OFF, zero behavior change).

## Problem

Factory orders increasingly span several repos: the client repo (contract/spec), the generated service repo (implementation PR), and the infra repo (deployment). The cowork DAG engine is single-workflow-centric: nodes run in one process against one workspace. Droid 2.0 parity gap (plan §0/§7).

## Goals

1. Nodes can declare a target repo; the scheduler fans out across repos with per-repo isolation.
2. Cross-repo dependency edges: a node in repo B can wait on an artifact produced by a stage in repo A.
3. Per-repo credentials resolved through the identity service at execution time — never present in workflow YAML.
4. Run records aggregate per-repo stage timings (feeds bench#70 durability + commerce#16 metering).

## Non-goals (v0.1)

- No distributed execution across machines — repos still execute in the cowork process; isolation is a per-repo workspace directory (`~/.fusion-cowork/repos/<url-hash>` worktree/clone cache).
- No cross-instance artifact transfer — cross-repo waits are intra-run DAG edges (see §3).

## 1. Workflow schema extension

No new engine schema: nodes declare the repo through `params.repo` (a plain dict, validated by the multi-repo layer when the flag is on):

```json
{
  "id": "service_pr",
  "name": "github_pr",
  "params": {
    "repo": {
      "url": "git@github.com:acme/service-api.git",
      "ref": "main",
      "token_source": "identity://acme/service-api/push"
    }
  }
}
```

`RepoSpec` fields:

| field | required | meaning |
|---|---|---|
| `url` | yes | clone URL; also the isolation cache key (sha256, first 12 hex) |
| `ref` | no (default `main`) | branch/tag the stage runs against |
| `token_source` | no | identity-service token URI (`identity://<tenant>/<token-name>`) resolved at execution time |

Nodes without `params.repo` are "local" stages and unaffected.

## 2. Secrets — identity tokens, never YAML

- Workflow YAML may only reference credentials through `token_source`. The multi-repo validator **rejects** any `repo` block carrying credential-looking keys (`token`, `password`, `secret`, `ssh_key`, `access_token` — literal or nested) with a `ValueError` at run admission, fail-closed.
- At execution time the engine resolves `identity://` URIs against fusion-identity (`FUSION_IDENTITY_URL`), exchanging the run's tenant context for a short-lived repo token. Resolution failure fails the node with an explicit error — no fallback to env literals.
- v0.1 ships the validator + resolution seam (`resolve_repo_token`); live identity exchange follows the existing `fusion_cowork/auth/jwt.py` verifier pattern.

## 3. Cross-repo dependency edges

Cross-repo waits are ordinary DAG edges plus an artifact gate:

- Repo A stage emits its artifact in `result.data` (e.g. `{"contract": {...}}`).
- Repo B node declares `waits_on_artifact: "<source_node_id>:<key>"` in `params`. The validator rewires/validates that a DAG edge from the source node exists (no edge = admission error), so the wait is the existing topological order — repo B's node simply does not start before repo A's artifact exists.
- Failure in repo A cancels repo B cleanly: the standard engine failure path already fails/skips all downstream nodes; `waits_on_artifact` nodes are downstream by construction, so a repo A failure leaves repo B nodes `SKIPPED` with the propagated error — never partially executed.

## 4. Isolation

Per-repo workspace: `~/.fusion-cowork/repos/<sha256(url)[:12]>` — a worktree/clone cache keyed by URL. Node executors that support `cwd` (shell, python REPL, github nodes) receive the repo workspace as their working directory when `params.repo` is present. Concurrent nodes on the same repo serialize on a per-repo lock (same-repo stages are ordered by the DAG anyway; the lock guards retry overlap).

## 5. Observability

`GET /api/runs/{run_id}` gains `repo_timings` when the run used multi-repo nodes:

```json
{
  "repo_timings": {
    "git@github.com:acme/client-app.git": {
      "stages": [{"node_id": "contract_test", "started_at": 1732..., "duration_s": 4.2}],
      "total_s": 4.2
    }
  }
}
```

Aggregation is a pure function over `WorkflowExecution.steps` + the node→repo map (`multi_repo.repo_timings`), so bench#70/commerce#16 can consume it without touching the engine.

## 6. Feature flag

`FUSION_COWORK_MULTI_REPO=1` enables: admission validation, token-resolution seam, repo workspace injection, `repo_timings` aggregation. Default OFF — workflows carrying `params.repo` run unchanged (repo block ignored) until the flag is on; the credential-literal rejection applies only when the flag is on, so existing workflows never regress.

## 7. Example — 3-repo DAG

```
[client repo]                    [service repo]                  [infra repo]
contract_extract ──► contract_test ──┐
                                     │ waits_on_artifact: contract_test:contract
                                     ▼
                                 service_impl ──► service_pr ──┐
                                                               │ waits_on_artifact: service_pr:pr_url
                                                               ▼
                                                           infra_apply ──► smoke_check
```

1. `contract_extract` + `contract_test` run in the client repo; `contract_test` emits `{"contract": <sha>}`.
2. `service_impl` / `service_pr` run in the service repo only after the contract artifact exists.
3. `infra_apply` / `smoke_check` run in the infra repo only after the PR URL artifact exists.

## 8. Acceptance criteria

- [x] Spec doc with example 3-repo DAG (this document).
- [x] Synthetic 2-repo run: repo B node waits on repo A artifact (`tests/test_issue_653_multi_repo.py::TestSyntheticTwoRepo::test_b_waits_on_a_artifact`); failure in repo A cancels B cleanly (`test_a_failure_cancels_b`).
- [x] Credential literal in `params.repo` rejected at admission (`test_credential_literal_rejected`).
- [x] Flag OFF = zero behavior change (`test_flag_off_ignores_repo_params`).
