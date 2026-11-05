# Cowork Web API Contract (v0.5.69)

`fusion-cowork web serve` (:11461, default) REST surface. Auth: `Authorization: Bearer <key>` or `x-api-key: <key>`; the key is `FUSION_COWORK_WEB_API_KEY` (static) or the gateway-issued `fm-` module key. Unauthenticated requests get `401 {"error":{"code":"unauthorized"}}`; unknown paths get FastAPI's plain `404 {"detail":"Not Found"}` — this document is the published contract, do not probe.

Convention: JSON bodies; errors are `{"detail": {"code": "...", "message": "..."}}` (except FastAPI's own 404/422).

## Runs & workflows

| Method | Path | Notes |
|---|---|---|
| POST | `/api/runs` | body: `workflow` (def) or `workflow_id`, optional `name/inputs/labels/source/memory/model`. Returns `{run_id, status}` |
| GET | `/api/runs` | list; `?status=&execution_id=&limit=` |
| GET | `/api/runs/{run_id}` | run record incl. `steps`, `hitl_pending`, `hitl_deadline` |
| POST | `/api/runs/{run_id}/pause` / `resume` / `cancel` | `409` when not pausable |
| POST | `/api/runs/{run_id}/directive` | `set_params` / `insert_node` / `skip_remaining` / `inject` |
| POST | `/api/runs/{run_id}/review` | delivery review `pending_review→approved/rejected` |
| GET | `/api/runs/{run_id}/events` | SSE snapshot + broadcast |
| GET | `/api/runs/{run_id}/artifacts` | derived from step outputs |
| POST | `/api/runs/{run_id}/resume-from` | body `{step_id}` → new linked run with snapshot |
| GET | `/api/runs/{run_id}/steps` | step list with tokens/duration |

## Permissions / approvals

| Method | Path | Notes |
|---|---|---|
| GET | `/api/permissions/pending` | merged approval inbox: local denials (incl. guard block/l4/fail-closed, issue #257) + guard L3; each entry has `action_id`, `tool_name`, `params`, `workflow`, `node_id`, `execution_id`, `diff_blocks`, `raw_edit` |
| POST | `/api/permissions/pending/{action_id}/confirm` | body `{approved: bool, once?: bool, scope?, accepted_blocks?, edited_params?}`; wakes the paused run by `execution_id` |
| GET | `/api/permissions/rules` / POST `/api/permissions/rules` | rule matrix CRUD |

## Nodes / templates / scheduler

| Method | Path | Notes |
|---|---|---|
| GET | `/api/nodes` | registered node types + params schema |
| GET/POST/DELETE | `/api/templates` `/api/templates/{id}` | templates CRUD |
| POST | `/api/templates/{id}/run` | run a template |
| GET/POST/PUT/DELETE | `/api/scheduler/tasks` (+ `/{id}`) | cron tasks |

## Skills (markdown packs)

| Method | Path | Notes |
|---|---|---|
| GET | `/api/skills` | list; items carry `editable` |
| GET | `/api/skills/{name}/source` | raw markdown (builtin: `editable:false`, empty source) |
| PUT | `/api/skills` | body `{markdown}` → validate + save to user dir, hot reload |
| DELETE | `/api/skills/{name}` | user packs only; builtin → `400 builtin` |
| POST | `/api/skills/{name}/run` | body `{params}` |

Note: Extensions → 技能 tab (`/api/extensions/skills*`) is the separate JSON SkillRegistry pack system, not this one.

## Workspaces / spaces

| Method | Path | Notes |
|---|---|---|
| GET | `/api/workspaces` | workspace list |
| GET | `/api/workspaces/system-dirs` | `?path=<relative>` subdirectory browse (containment-checked; escape `400 bad_path`, missing/file `404 not_found`) |
| GET/POST/DELETE | `/api/space/spaces*` | spaces + messages + artifacts under `/api/space/*` |
| GET/PUT | `/api/space/spaces/{id}/instructions` | space instructions |
| GET | `/api/space/notifications` | notification feed |

## Observability / admin

| Method | Path | Notes |
|---|---|---|
| GET | `/api/sessions?source=engine|space|scheduler` | unified sessions |
| GET | `/api/usage` | token/call usage |
| GET | `/api/search?q=` | aggregated search (manifest searchProviders) |
| GET | `/api/notifications/approvals` + `/api/notifications/runs` | manifest notification sources |
| GET | `/api/models` | chat models; `?all=1` raw list |
| GET | `/api/mobile/overview` + POST | `/api/deliver` |
| POST | `/api/admin/*` (`x-admin-key`) | OTLP audit export, push test, admin usage |

## Engine knobs (env)

| Env | Default | Meaning |
|---|---|---|
| `FUSION_COWORK_HITL_PAUSE` | on | absent/other → pause on pending approval; `0/false/off/no` → headless deny-fast |
| `FUSION_COWORK_HITL_TIMEOUT` | 1800 | approval wait bound (`s/m/h` suffix); timeout auto-denies |
| `FUSION_COWORK_NODE_TIMEOUT_S` | 1800 | per-node watchdog default (nodes may override via `config.timeout`; `0` disables) |
| `FUSION_COWORK_NODE_HEARTBEAT_S` | 30 | PROGRESS heartbeat interval during long nodes |
| `FUSION_COWORK_WORKSPACE_ROOT` | home | root for system-dir browsing |
| `FUSION_COWORK_TRUSTED_SYNC_URLS` | agent-studio approvals endpoint | issue #301: comma-separated URL prefixes seeded as `http_request` allow rules (deterministic template sync targets — no per-node confirm on fresh deployments); `off/none/false/0` disables; persisted deny rules always win |
