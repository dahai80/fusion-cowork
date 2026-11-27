"""issue #544: cowork module API IDOR regression matrix — cross-tenant actors must never get 2xx.

Module api-key chain model: a single module key (env static key / gateway-issued fm- key) = admin;
auth is centralized in _auth_middleware (401); there is no per-endpoint auth to bypass.

Matrix (resource x actor):
  resource: workflows (list/get/put/delete) / runs (list/get/directive/pause)
            / artifacts / run events (logs) / scheduler tasks
  actor:    (a) another tenant's api-key (any non-accepted key)
            (b) another tenant's user JWT (identity token is not a module API credential)
            (c) no auth
            (d) revoked api-key (after a module key is revoked the old value must be rejected)
Per-cell verdict: 401/403/404 — never 200/201/202 with data.
"""

from __future__ import annotations

import pytest

VALID_BODY = {"workflow": {"nodes": [], "edges": []}, "name": "idor-probe"}

# (method, path, json_body) — one representative endpoint per resource
MATRIX_ENDPOINTS = [
    ("GET", "/api/workflows", None),
    ("GET", "/api/workflows/wf_foreign_tenant", None),
    ("PUT", "/api/workflows/wf_foreign_tenant", VALID_BODY),
    ("DELETE", "/api/workflows/wf_foreign_tenant", None),
    ("GET", "/api/runs", None),
    ("GET", "/api/runs/run_foreign_tenant", None),
    ("POST", "/api/runs/run_foreign_tenant/directive", {"action": "set_params", "params": {}}),
    ("POST", "/api/runs/run_foreign_tenant/pause", None),
    ("GET", "/api/runs/run_foreign_tenant/artifacts", None),
    ("GET", "/api/runs/run_foreign_tenant/events", None),
    ("GET", "/api/artifacts/refs/att_foreign_tenant", None),
    ("GET", "/api/templates", None),
    ("GET", "/api/scheduler/tasks", None),
    ("DELETE", "/api/scheduler/tasks/task_foreign_tenant", None),
]

ACTOR_HEADERS = {
    "foreign_api_key": {"Authorization": "Bearer fk_other_tenant_module_key_000"},
    "foreign_user_jwt": {
        "Authorization": "Bearer eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJvdGhlci10ZW5hbnQtdXNlciIsInRlbmFudF9pZCI6Im90aGVyIn0.sig"
    },
    "no_auth": {},
}


@pytest.fixture
def idor_client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from fusion_cowork.engine.workflow import WorkflowEngine
    from fusion_cowork.server import web_api

    monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", "valid-module-key-544")
    monkeypatch.setattr(web_api, "_module_key", lambda: "")
    eng = WorkflowEngine(hitl_pause=False)
    app = web_api.create_web_app(engine=eng, dist_dir=tmp_path / "no-dist", store_root=tmp_path / "store")
    with TestClient(app) as client:
        yield client


@pytest.mark.parametrize("method,path,body", MATRIX_ENDPOINTS)
@pytest.mark.parametrize("actor,headers", list(ACTOR_HEADERS.items()))
def test_foreign_actor_never_reads_tenant_data(idor_client, method, path, body, actor, headers):
    resp = idor_client.request(method, path, json=body, headers=headers)
    assert resp.status_code in (401, 403, 404), (
        f"IDOR matrix [{actor}] {method} {path}: expected 401/403/404, got {resp.status_code}: {resp.text[:200]}"
    )


def test_revoked_api_key_rejected_everywhere(idor_client, monkeypatch):
    # (d) revocation/rotation: the deployment still has a key configured (auth_configured must be True —
    # an empty config follows the #146 "unset = allow" dev contract, which is not revocation semantics),
    # and the old key must be rejected on every resource.
    # Rotation model: env static key swapped to a new value; module fm- key revocation: _module_key returns empty
    from fusion_cowork.server import web_api

    old_key = {"Authorization": "Bearer valid-module-key-544"}
    assert web_api.auth_configured()
    monkeypatch.setattr(web_api, "_api_key", lambda: "rotated-module-key-544")
    monkeypatch.setattr(web_api, "_module_key", lambda: "")
    assert web_api.auth_configured(), "rotation must keep the deployment auth-configured"
    for method, path, body in MATRIX_ENDPOINTS:
        resp = idor_client.request(method, path, json=body, headers=old_key)
        assert resp.status_code in (401, 403, 404), (
            f"revoked key {method} {path}: expected 401/403/404, got {resp.status_code}"
        )
    # new key works (rotation does not lock the deployment out)
    resp = idor_client.get("/api/workflows", headers={"Authorization": "Bearer rotated-module-key-544"})
    assert resp.status_code == 200


def test_valid_key_still_allowed(idor_client):
    # control group: a valid key reads normally (the matrix must not block correct traffic too)
    resp = idor_client.get("/api/workflows", headers={"Authorization": "Bearer valid-module-key-544"})
    assert resp.status_code == 200
    assert "workflows" in resp.json()
