# MCP Server Registry & Credential Conventions (Consumed-Server Side)

This document covers how fusion-cowork **consumes** external MCP tool servers from DAGs:
the approved-server inventory, per-scope approval, credential issuance for consumed
servers, warn-level registry enforcement, and the correlation-id audit contract.
For cowork's *own* MCP server (16 built-in tools, stdio/HTTP `:11438`) see
[api.md](api.md); for the identity/key model see [identity.md](identity.md).

## 1. Approved-server inventory

The machine-readable source of truth lives in the monorepo:
[`architecture/mcp-server-registry.json`](https://github.com/dahai80/fusion/blob/main/architecture/mcp-server-registry.json)
(version 1, `servers[]` with name/transport/port/purpose/credential_mode/status/
url_prefixes/tool_scopes). The human summary table is
`architecture/mcp-server-registry-1029.md` — keep both in sync when editing.

Resolution order (owned by `fusion_core.mcp_registry`, fusion-core >= 0.9.0):

1. `FUSION_MCP_REGISTRY` env var (absolute path to a registry JSON)
2. `architecture/mcp-server-registry.json` in the monorepo checkout
3. Bundled copy shipped inside `fusion_core`

The file is mtime-cached, so registry edits are picked up at runtime without restart.

Server lifecycle statuses: `proposed` → `reviewed` → `approved` → `deprecated`.
Only `approved` servers may be referenced without an explicit operator override.
Credential modes: `identity_issuance` (keys minted by the identity/gateway layer,
never stored in workflow definitions) and `env_ref` (credentials referenced via
environment variable or keychain reference, resolved at launch).

## 2. Per-scope approval (operator side)

The `mcp_client` node is HIGH_RISK: default deny, explicit approval required.
Permission rules support the `url:` prefix scope to approve a single server
(same prefix semantics as the registry — trailing slashes normalized,
longest prefix wins):

```python
permission_manager.approve("mcp_client", scope="url:http://127.0.0.1:11500/mcp")
```

Via the web console (Approvals page) the same scope is offered as
"always allow" on the pending card. An operator approval overrides a
non-`approved` registry verdict — the registry check warns, it does not block
(see §4).

## 3. Credential issuance for consumed servers

Rule: **no self-carried keys** — workflow definitions and node params never embed
secrets for consumed MCP servers. Credentials reach a consumed server through one
of two paths, matching the `credential_mode` on its registry record:

- **`identity_issuance`** — the platform identity layer (fusion-identity /
  gateway `fm-` keys) mints a scoped credential; cowork forwards it via the
  node's `headers` param, which the *launcher* (the business app or runbook)
  injects from its own secret store at submission time — not stored in the
  workflow def.
- **`env_ref`** — the consumed server itself reads its credentials from
  environment variables or a macOS keychain reference at launch; cowork nodes
  carry only the URL and tool arguments. Example: a Google Sheets MCP server
  launched with `SHEETS_OAUTH_TOKEN` injected by the operator's launch
  environment; DAG nodes call `sheets.read` with no credential material at all.

Consistent with [identity.md](identity.md) (JWT issuance for cowork's own API)
and the skill-pack policy (admin-only script packs, `fm-` module keys): secrets
always arrive via environment/identity indirection, never inline in stored
definitions.

## 4. Registry check on connect (warn-level)

On every `mcp_client` execution, before the Streamable HTTP `initialize`
handshake, the node calls:

```python
from fusion_core import check_approval

verdict = check_approval(url, tool_scope=tool)
# verdict.action: "approved" | "denied" | "unknown"
```

- `approved` → INFO audit line, proceeds
- `denied` / `unknown` → **WARNING** with `verdict.reason` + correlation id;
  the node still proceeds — the HIGH_RISK permission gate (with `url:` scope
  operator approval) is the enforcement point; the registry check is
  observability for governance drift
- registry unreadable / `fusion_core` absent (bare CI install) → check skipped
  (debug log), no behavior change

Override: an operator `approve("mcp_client", scope="url:...")` rule makes the
call legitimate even when the registry verdict is `unknown` — the warning is
expected in that case and can be silenced by adding the server to the registry.

## 5. Audit contract (correlation ids)

Every `mcp_client` invocation emits a per-invocation audit log line containing
the registry verdict, server name, tool scope, and a correlation id:

```
mcp_client: registry denied server=sheets-dev tool=sheets.read \
    reason="server status is 'reviewed', only approved servers may be referenced" \
    correlation_id=fc_1a2b3c4d5e6f7a8b
```

The correlation id is the observability trace id
(`fusion_cowork.observability.trace`, `fc_`-prefixed 16-hex). Business pipelines
that need a per-task audit trail (e.g. document-automation jobs) propagate the
same trace id across their run, so an external tool call traces back to the
task that caused it via `grep correlation_id=<id>`.
