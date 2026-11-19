"""issue #544: cowork module API IDOR 回归矩阵 — 跨租户 actor 永不 2xx。

模块 api-key 链模型: 单模块密钥 (env 静态 key / 网关签发 fm- key) = admin,
认证集中在 _auth_middleware (401), 无 per-endpoint 鉴权可绕。

矩阵 (resource × actor):
  resource: workflows (list/get/put/delete) / runs (list/get/directive/pause)
            / artifacts / run events (logs) / scheduler tasks
  actor:    (a) 另一租户的 api-key (任意非接受 key)
            (b) 另一租户的 user JWT (identity token 不是模块 API 凭证)
            (c) 无认证
            (d) 已吊销的 api-key (模块密钥吊销后旧值必须被拒)
每格判定: 401/403/404 — 绝不 200/201/202 带数据。
"""

from __future__ import annotations

import pytest

VALID_BODY = {"workflow": {"nodes": [], "edges": []}, "name": "idor-probe"}

# (method, path, json_body) — 每个资源一条代表端点
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
    # (d) 吊销/轮换: 部署仍配置密钥 (auth_configured 必须为 True — 空配置走
    # #146 "unset = allow" 开发契约, 不属吊销语义), 旧 key 在每个资源上都必须被拒。
    # 轮换模型: env 静态 key 换新值; 模块 fm- key 吊销: _module_key 返回空
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
    # 新 key 正常 (轮换不锁死)
    resp = idor_client.get("/api/workflows", headers={"Authorization": "Bearer rotated-module-key-544"})
    assert resp.status_code == 200


def test_valid_key_still_allowed(idor_client):
    # 对照组: 合法 key 正常读取 (矩阵不把正确流量也挡掉)
    resp = idor_client.get("/api/workflows", headers={"Authorization": "Bearer valid-module-key-544"})
    assert resp.status_code == 200
    assert "workflows" in resp.json()
