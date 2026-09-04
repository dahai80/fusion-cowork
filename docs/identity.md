# fusion-identity Integration: Production Contract

> Issue #90 — production identity path, env defaults, and the HTTP/UDS contract
> fusion-studio (and any client) should use when `FUSION_IDENTITY_ENABLED=1`.

fusion-identity is the **single JWT issuer + tenant registry** for the Fusion
ecosystem. fusion-cowork delegates JWT verification, tenant resolution, and
quota enforcement to it when the opt-in flag is set. Default OFF = zero
behavior change (local-first single-machine dev path preserved).

## 1. Opt-in

| Env | Default | Required in prod | Purpose |
|---|---|---|---|
| `FUSION_IDENTITY_ENABLED` | unset (`0`) | `1` | Activate identity-backed verify + tenant middleware |
| `FUSION_IDENTITY_URL` | `http://127.0.0.1:11470` | yes (if non-loopback) | fusion-identity service URL |
| `FUSION_IDENTITY_SERVICE_TOKEN` | unset | `1` (required) | Service token gating `POST /api/v1/auth/verify` |
| `FUSION_REQUIRE_JWT` | unset | `1` | Fail-closed: reject any request with no/invalid credential (no silent fallback to `local_user`) |

`FUSION_IDENTITY_SERVICE_TOKEN` is **required** when `FUSION_IDENTITY_ENABLED=1`;
without it `get_identity_client()` returns `None` and the identity path no-ops
(logs a warning). In production set `FUSION_REQUIRE_JWT=1` alongside it so an
unreachable identity service returns 401 rather than silently degrading to the
local static-token / `local_user` path.

## 2. Deploy topology

```
fusion-studio (Swift GUI)
   │  HTTP: Authorization: Bearer <jwt> + X-Tenant-Id: <tid>
   │  UDS:  JSON-RPC params._auth_token = <jwt>, params.x-user-id = <uid>
   ▼
fusion-cowork  (FastAPI :11438/11439, MCP :11438, UDS /tmp/fusion-cowork.sock)
   │  sync httpx.Client POST /api/v1/auth/verify  (Bearer <service_token>)
   ▼
fusion-identity  (HTTP :11470)  — sole JWT issuer + tenant registry
```

- fusion-identity runs on `127.0.0.1:11470` (loopback only — cowork calls it
  via sync `httpx.Client` from inside request handling).
- fusion-cowork never issues tokens; it only **verifies** them against
  fusion-identity. The `FUSION_IDENTITY_SERVICE_TOKEN` is the cowork→identity
  service credential, **not** a user token.
- jti→claims cache (TTL 60s, cap 1024) cuts repeat `/verify` calls; a
  `revoked=true` or `tenant_status!=active` response is fail-closed (401).

## 3. HTTP contract (FastAPI apps: space API, MCP HTTP/streamable)

When `FUSION_IDENTITY_ENABLED=1`, the space API installs
`fusion_core.tenant.install_tenant_middleware` (require_jwt=True) + a cowork
contextvar bridge. Enforced headers:

| Header | Required | Notes |
|---|---|---|
| `Authorization: Bearer <jwt>` | yes (non-exempt paths) | JWT issued by fusion-identity |
| `X-Tenant-Id: <tid>` | yes | Must equal `jwt.tid` claim, else 401 |
| `X-User-Id: <uid>` | optional | identity verify returns no uid; cowork reads uid from this header (fallback `local_user`) |

Exempt paths (no token required): `/health`, `/docs`, `/openapi.json`, `/redoc`,
and for MCP `/mcp`, `/sse`.

Flow: middleware verifies the Bearer JWT against fusion-identity, checks
`claims.tid == X-Tenant-Id`, sets `fusion_core.tenant.TenantContext`. The cowork
bridge then propagates `tctx.tenant_id`/`tctx.user_id` into the cowork
`_tenant_ctx` contextvar that 28+ downstream sites read via
`get_current_tenant()`.

**This is the contract fusion-studio should use for all HTTP calls.**

## 4. UDS contract (DeskRPC JSON-RPC `/tmp/fusion-cowork.sock`)

UDS has no HTTP middleware; `DeskRPCServer._authenticate` verifies the token
manually. The JSON-RPC `params` object carries identity:

| params field | Required | Notes |
|---|---|---|
| `_auth_token` | yes (prod) | **Raw JWT string**, NOT `Bearer`-prefixed |
| `x-user-id` (or `X-User-Id` / `_user_id`) | optional | uid; identity returns no uid, so cowork reads it here (fallback `local_user`) |

`tid` is **not** taken from params — it is taken from the identity verify
response (`result.tid`), which is the trusted source. Identity fields in params
(`operator_id`/`user_id`/`inviter_id`/`author_id`/`owner_id`/`from_user_id`)
are stripped and replaced with the connection-level principal (anti-IDOR).

**Mismatch with fusion-studio's `_auth: { jwt, tid }`:** fusion-cowork reads a
**flat** `_auth_token` string, not a nested `_auth.jwt` object. To align, the
studio's UDS bridge must send:

```jsonc
// JSON-RPC params
{
  "_auth_token": "<jwt>",
  "x-user-id": "<uid>",
  ...method args
}
```

not `{"_auth": {"jwt": "...", "tid": "..."}}`. The `tid` inside `_auth` is
ignored — cowork trusts only `result.tid` from `/verify`. (If the studio keeps
the nested shape, a thin adapter in the studio bridge that flattens
`_auth.jwt` → `_auth_token` resolves it without cowork changes.)

## 5. WebSocket / sync / remote paths

WS handshake (`/spaces/{id}/ws`), `remote.py`, `sync.py` go through
`auth/fallback.py::verify_any_token` — the identity-aware seam. When identity
is enabled, `verify_any_token` delegates to `IdentityClient.verify(token)` and
fail-closes on revoked/unreachable (no static-token fallback in prod mode). No
`X-Tenant-Id` header enforcement on these paths; tenant comes from the JWT
`tid` claim. Disabled = current JWT-then-static behavior.

## 6. Quotas

When identity is enabled, `QuotaEnforcer(identity_client=...)` reads quotas
from the cached `VerifyResponse.quota` dict (returned on every `/verify`,
cached per-jti) instead of ConfigCenter. Usage is reported best-effort via
`POST /api/v1/tenants/{tid}/usage` (failures logged, not fatal).

## 7. Disabled (default) — local-first dev

No `FUSION_IDENTITY_ENABLED` → identity path no-ops. `get_default_verifier()`
returns the local `JWTVerifier.from_env()` (HS256/RS256 via
`FUSION_JWT_SECRET`/`FUSION_JWKS_URL`), static-token fallback works, and the
local `local_user`/`default` tenant path is preserved. Zero HTTP calls to
fusion-identity.
