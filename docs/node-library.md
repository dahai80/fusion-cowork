---
doc_id: fusion-cowork/guide/node-library
product: fusion-cowork
class: platform
entry: service
doc_type: guide
title: {zh: "节点库目录", en: "Node library catalog"}
audience: engineer
status: draft
version: 0.5.85
updated: 2026-11-07
owner: @dahai
source_of_truth:
  - docs/node-library.md
---

# Official Node Library (issue #262)

Five first-party, generic node families for customer flows. All configuration is per-flow-instance data — node code contains zero use-case constants. Tokens travel via environment variables only and never appear in flow definitions or logs.

Nodes live in `fusion_cowork/nodes/ops/ops_nodes.py` (category `tool`). They self-register at import time; `import_all_nodes()` loads them, so the web node palette and `/api/nodes` list them automatically.

Contribution contract: subclass `BaseNode`, decorate with `@register_node`, declare `get_params_schema()` (JSON Schema — the canvas inspector renders it), return structured `NodeResult`s, and log decisions. Security-sensitive nodes must be added to `HIGH_RISK_NODES` (`fusion_cowork/engine/permission_manager.py`) so the platform approval gate applies.

## agent_exec

Launches an external headless agent/CLI process (argv, never a shell), captures exit code and output tail.

| Param | Type | Notes |
|---|---|---|
| `command` | string[] | argv list; required |
| `cwd` | string | must exist |
| `timeout` | number | default 120s; timeout kills the process |
| `env_keys` | string[] | environment variable names forwarded to the child |
| `max_output` | number | output tail kept, default 20000 chars |

Output: `exit_code`, `output`. Crash (signal) fails with a structured message — relaunch policy is re-running the node; durable state belongs in an external store (process lifecycle is delegated to the agent-execution surface).

## github_api

Issue/PR/label operations against the GitHub REST API. `GITHUB_TOKEN` env is the only credential channel.

| Param | Type | Notes |
|---|---|---|
| `op` | enum | `issue_create` / `issue_comment` / `issue_set_labels` / `pr_get` / `list_labels` |
| `repo` | string | `org/repo`, must be in `allowed_repos` |
| `allowed_repos` | string[] | whitelist; anything else is refused loudly |
| `issue_number` | number | for comment/labels/pr_get |
| `title` / `body` / `labels` | — | payloads per op |

`FUSION_GITHUB_API` overrides the API base (GHES / testing).

## command_evidence

Whitelist-based command execution with evidence archiving.

- `allowlist` (string[], fnmatch patterns matched against the joined argv in both absolute and basename form) — empty allowlist or non-matching command is refused loudly, never silently allowed.
- stdout+stderr are scrubbed (built-in secret patterns: `ghp_*`, `sk-*`, `AKIA…`, `Bearer …`, `password=…`, plus custom `redact` regexes) and written to `evidence_dir/<ts>-<argv0>.txt`; the command line itself is scrubbed too.
- `ports` + `port_registry` (JSON `{port: expected-service-up}`) produce a reconciliation report (`expected` vs live TCP probe); mismatches are logged as warnings.

## http_webhook

Calls an external HTTP API with retry and outbound HMAC signing.

| Param | Type | Notes |
|---|---|---|
| `url` | string | http/https only |
| `method` | enum | GET/POST/PUT |
| `headers` | object | values support `${ENV_NAME}` runtime interpolation |
| `retry` / `retry_backoff` | number | retries on network errors and 5xx only (4xx fails immediately) |
| `sign_secret_env` | string | env var holding the HMAC secret (default `FUSION_COWORK_WEBHOOK_SECRET`); when set, requests carry `X-Fusion-Signature: sha256=<hex>` over the raw body |

**Inbound callback → resume a gated flow:** pair an `approval_gate` node with the signed callback route

```
POST /api/hooks/webhook/{run_id}
X-Fusion-Signature: sha256=<hex hmac-sha256 of raw body with FUSION_COWORK_WEBHOOK_SECRET>
{"approved": true, "params": {"k": "v"}}
```

The route verifies the HMAC (static API key auth is bypassed for `/api/hooks/*` — the signature is the auth), finds the run's pending approval, and resumes it with the decision and optional edited params. `{"resumed": false}` is returned when the run has nothing pending.

## approval_gate

Pauses the flow for a human decision. The node registers in `HIGH_RISK_NODES`, so the platform permission layer denies it by default and the run parks at the HITL gate — visible as a `blocked` step in the timeline (issue #260) with a countdown in the approval inbox. Decisions arrive via the web approval card (approve once / always allow / deny, optional edited params) or the signed webhook route above. If approved, the node simply passes its input through with `approved: true`.
