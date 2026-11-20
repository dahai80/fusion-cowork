---
doc_id: fusion-cowork/engine-service-contract
product: fusion-cowork
class: platform
entry: service
doc_type: guide
title: {zh: "引擎服务调用契约", en: "Engine service contract (storefront bff)"}
audience: engineer
status: draft
version: 0.5.136
updated: 2026-11-20
owner: "@dahai"
source_of_truth:
  - docs/engine-service-contract.md
---

# Engine Service Contract (storefront bff → fusion-cowork)

Issue #565. How the fusion-site storefront bff (bench-site) triggers the
fusion-cowork engine over HTTP with a dedicated service credential — no
client-side simulation.

## 1. Credential path

| Env var | Set on | Purpose |
|---|---|---|
| `FUSION_COWORK_URL` | site bff | Engine web API base, e.g. `http://127.0.0.1:11461` |
| `FUSION_COWORK_SERVICE_KEY` | site bff **and** fusion-cowork serve | Dedicated service-to-service key (not a user key) |

- Provisioning: generate a high-entropy secret per node (TX + ali), inject via
  env/secret on BOTH sides (bff sends it, cowork serve accepts it). Same value
  on both nodes' env for the same cowork instance.
- The key is accepted in addition to the existing credentials
  (`FUSION_COWORK_WEB_API_KEY` static user key + gateway-issued `fm-` module
  key). Setting it changes nothing for existing callers.
- **Scope**: the service key is only valid on `/api/runs*` endpoints (create /
  poll / retry / cancel / directives). Any other `/api/*` path returns
  `403 {"error": {"code": "forbidden", ...}}`. Workflow definitions, approvals
  and space APIs stay user-key territory.
- Transport: `Authorization: Bearer <FUSION_COWORK_SERVICE_KEY>` or
  `x-api-key: <FUSION_COWORK_SERVICE_KEY>`.

## 2. Endpoints used by the bff

All under `FUSION_COWORK_URL`:

| Operation | Call | Notes |
|---|---|---|
| Create run | `POST /api/runs` | body `{"workflow_id": "product_lifecycle_agents", "inputs": {"order_id": "...", "prd": "..."}}` → `202 {"run_id": ...}`. `workflow_id` resolves the seeded workflow definition (template seeding, issue #485); pass `workflow` inline instead when the bff owns the definition. Arbitrary bff context (order id, PRD text) goes in `inputs` — injected into the first node. |
| Poll run | `GET /api/runs/{run_id}` | status + `steps[]` per node: `status`, `node_name`, `started_at`, `duration`, `input_data`, `output_data`, `error` (stage state + evidence). Terminal states: `completed` / `failed` / `cancelled` / `completed_with_denials`. |
| Retry one stage | `POST /api/runs/{run_id}/resume-from` | body `{"node_id": "<node_id>"}` → new linked run replaying the snapshot up to that node and re-executing from it (failed/terminal runs only, `409 not_terminal` otherwise). |
| Cancel | `POST /api/runs/{run_id}/cancel` | best-effort stop. |
| Directives (optional) | `POST /api/runs/{run_id}/directive` | `set_params` / `insert_node` / `skip_remaining` at node boundaries. |

Full reference: `docs/api-reference.md`.

## 3. Minimal bff flow

```python
import httpx

base = os.environ["FUSION_COWORK_URL"]
key = os.environ["FUSION_COWORK_SERVICE_KEY"]
headers = {"Authorization": f"Bearer {key}"}

resp = httpx.post(
    f"{base}/api/runs",
    headers=headers,
    json={"workflow_id": "product_lifecycle_agents",
          "inputs": {"order_id": order_id, "prd": prd_text}},
)
run_id = resp.json()["run_id"]

while True:
    rec = httpx.get(f"{base}/api/runs/{run_id}", headers=headers).json()
    if rec["status"] in ("completed", "failed", "cancelled", "completed_with_denials"):
        break
    time.sleep(2)

failed = [s for s in rec["steps"] if s["status"] == "failed"]
if failed:
    httpx.post(f"{base}/api/runs/{run_id}/resume-from",
               headers=headers, json={"node_id": failed[-1]["node_id"]})
```

## 4. Failure semantics

- `401` = key missing/invalid (check both sides' env). `403` = valid service
  key used outside `/api/runs*`.
- HITL: gated nodes pause the run (`status=running`, `hitl_pending=true`) until
  an approval is resolved via the approvals API (user key) — the service key
  cannot approve. Headless deployments can set `FUSION_COWORK_HITL_PAUSE=0` on
  the serve process for deny-fast behavior.
- Polling a crashed run: run records are durable (`~/.fusion-cowork/web/runs`);
  after a serve restart crashed runs re-register as `paused` and need a manual
  `POST /api/runs/{run_id}/resume`.
