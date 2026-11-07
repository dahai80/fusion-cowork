---
doc_id: fusion-cowork/guide/capability-marketplace
product: fusion-cowork
class: platform
entry: service
doc_type: guide
title: {zh: "能力市场", en: "Capability marketplace"}
audience: engineer
status: draft
version: 0.5.85
updated: 2026-11-07
owner: @dahai
source_of_truth:
  - docs/capability-marketplace.md
---

# Capability Marketplace (issue #228)

Workspace-scoped pool of skills, connectors and subagents. Installing a
capability into a workspace makes it visible to the task composer; runs are
gated by what the workspace installed and what scopes it granted.

## Data model

**Skill**: `skill.<name>` (built-in, JSON user pack, markdown package).
**Connector**: `connector.<id>` from a user connector config
(`~/.fusion-cowork/connectors/*.json`), `connector.knowledge.<kb>` for
fusion-rag wired KBs, or `connector.<id>` when `fusion_core.mcp_registry`
is installed.
**Subagent**: `subagent.template.<id>` (DAG template),
`subagent.graph.<gid>` / `subagent.agent.<aid>` (agent-studio).

Pool state lives in `~/.fusion-cowork/capabilities.json`
(`FUSION_COWORK_CAPABILITIES_PATH` overrides), keyed by workspace id:

```json
{
  "workspaces": {
    "space_123": {
      "skill": { "skill.echo": { "installed_at": 0, "enabled": true, "usage": {}, "audit": [] } },
      "connector": {},
      "subagent": {}
    }
  }
}
```

The default workspace (`""` / `default`) needs no space record; any other
workspace id is validated against `SpaceStore` (404 when missing).

## HTTP API

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/capabilities?workspace_id=&q=&type=` | catalog + pool state |
| POST | `/api/capabilities/{type}/{id}/install` | install into workspace |
| DELETE | `/api/capabilities/{type}/{id}?workspace_id=` | uninstall |
| PATCH | `/api/capabilities/{type}/{id}/enabled` | enable / disable |
| POST | `/api/capabilities/connectors/{id}/scopes` | grant scopes (`approve:true` required for sensitive) |
| POST | `/api/capabilities/connectors/{id}/oauth/start` | build authorize URL |
| GET | `/api/capabilities/oauth/callback` | exchange code, store encrypted tokens |
| POST | `/api/capabilities/connectors/{id}/revoke` | clear credentials + scopes |
| GET | `/api/capabilities/{type}/{id}/usage` | usage counters + audit trail |
| GET | `/api/capabilities/subagents/{id}/definition` | template / agent-studio definition |
| POST | `/api/capabilities/subagents/{id}/spawn` | start a run or agent-studio execution |

`GET /api/search` now also returns capability hits (`kind:"capability"`,
route `/capabilities/<type>`); the web shell's ⌘K palette consumes it.

## Run gating

`PermissionManager.capability_gate` is invoked from `check()` after approve /
deny rules and before the high-risk check:

- skill tools (`skill.<name>`) — denied when the workspace has installed
  skills but this one is missing or disabled.
- `mcp_client` — resolved to a connector by longest URL-prefix match. The
  connector gate only activates once the workspace has installed at least one
  connector (legacy workspaces keep working). Missing or disabled →
  deny. Pending sensitive scopes matching the tool, or an OAuth connector
  without credentials → approval (reuses the HITL pending queue).
- Everything else is untouched.

The engine records the workspace for each execution
(`WorkflowEngine.set_run_workspace`), so the gate reads it from the approval
context. `RunRegistry.capability_recorder` writes usage + audit rows when a
run ends; verdicts are `denied`, `gated`, or the run status.

## OAuth

Connector config declares:

```json
{
  "id": "sheets",
  "name": "Google Sheets",
  "url": "http://127.0.0.1:7777",
  "scopes": [{ "id": "sheets.read" }, { "id": "sheets.write", "sensitive": true }],
  "oauth": {
    "authorize_url": "https://accounts.example.com/authorize",
    "token_url": "https://accounts.example.com/token",
    "client_id_env": "SHEETS_OAUTH_CLIENT_ID",
    "client_secret_env": "SHEETS_OAUTH_CLIENT_SECRET",
    "scopes": ["sheets.read", "sheets.write"]
  }
}
```

Authorization-code flow with optional PKCE (S256, on by default), `state`
TTL 600 s, in-memory pending map. Tokens are stored via `encrypt_at_rest`
(Fernet when `FUSION_ENCRYPTION_KEY` is set, plaintext with a warning
otherwise) and never logged.

## Subagents

Definition preview reads the DAG template or upstream agent-studio
(`GET /v1/graphs/{id}`, `GET /v1/agents/{id}/definition`). Spawn either starts
a local run (`POST /api/runs` with `space_id`) or executes upstream
(`POST /v1/graphs/{id}/execute` with `graph_id`, `POST /v1/runs` with
`goal`). `spawn_policy.requires_approval` is enforced: requests must carry
`approve: true` or the route returns 409 `spawn_approval_required`; the web
page confirms before sending it.

## Web UI

- `#/capabilities` (`CapabilitiesPage`) — tabs Skills / Connectors /
  Subagents, workspace selector, install/enable/uninstall, scope grant
  (sensitive scopes confirm), OAuth connect/revoke, usage audit, subagent
  definition + spawn.
- Task home `⚙` capability picker reads the workspace pool.
- ⌘K palette (`CommandPalette`) searches workflows, runs, spaces and
  capabilities through `GET /api/search`.

## Tests

- `tests/test_issue_228_capability_marketplace.py` — catalog sources,
  workspace isolation, install/enable/uninstall, gate verdicts, sensitive
  scope approval, OAuth start/callback/revoke, usage audit, search,
  subagent definition/spawn, permission-manager integration.
- `web/tests/capabilities228.test.tsx` — tab mapping, filters, pool helpers.
