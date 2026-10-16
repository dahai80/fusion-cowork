"""#94: union-web 模块 — web_api REST 面 / 静态密钥鉴权 / 运行生命周期 / manifest。

Contract:
- FUSION_COWORK_WEB_API_KEY 配置后, /api/* 401 无密钥, Bearer 或 x-api-key
  接受静态密钥 (gateway /shell 代理注入路径), 错误密钥 401; /health 免鉴权。
- 工作流定义 CRUD 走隔离的临时目录 (WorkflowDefStore root 注入)。
- 运行生命周期: 提交 (202 running) → 轮询至终态 → cancel 未运行项 404。
- GET / 托管 web/dist SPA (未构建则跳过); manifest.json 字段符合 PRD §5.1。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

STATIC_KEY = "fc-web-test-94"


@pytest.fixture
def static_key_env(monkeypatch):
    monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", STATIC_KEY)
    return STATIC_KEY


@pytest.fixture
def store_root(monkeypatch, tmp_path):
    root = tmp_path / "workflows"
    from fusion_cowork.server import web_api

    monkeypatch.setattr(web_api, "_WEB_DIR", tmp_path / "web")
    return root


@pytest.fixture
def client(static_key_env, store_root, tmp_path):
    from fastapi.testclient import TestClient

    from fusion_cowork.engine import WorkflowEngine
    from fusion_cowork.engine.node import NodeRegistry
    from fusion_cowork.server.web_api import create_web_app

    # mock 节点注册 (保存/恢复 registry 状态, 对齐测试约定)
    saved = dict(NodeRegistry._registry)
    try:
        from fusion_cowork.engine.node import BaseNode, NodeCategory, NodeResult, NodeStatus, register_node

        @register_node
        class MockWebNode(BaseNode):
            name = "mock_web_node"
            category = NodeCategory.TOOL

            async def execute(self, params):
                return NodeResult(
                    status=NodeStatus.SUCCESS,
                    data={"message": "hello-94"},
                    summary="mock ok",
                )

        engine = WorkflowEngine()
        app = create_web_app(engine=engine, dist_dir=tmp_path / "no-dist", store_root=store_root)
        yield TestClient(app)
    finally:
        NodeRegistry._registry.clear()
        NodeRegistry._registry.update(saved)


class TestStaticKeyAuth:
    def test_check_static_key_match(self, static_key_env):
        from fusion_cowork.server.web_api import check_static_key

        result = check_static_key(STATIC_KEY)
        assert result is not None
        assert result["valid"] is True
        assert result["key_id"] == "static-env"

    def test_check_static_key_mismatch(self, static_key_env):
        from fusion_cowork.server.web_api import check_static_key

        assert check_static_key("fc-wrong") is None
        assert check_static_key("") is None

    def test_extract_caller_key_bearer_and_header(self):
        from fusion_cowork.server.web_api import extract_caller_key

        class _FakeHeaders(dict):
            def get(self, k, default=""):
                return super().get(k.lower(), default)

        h = _FakeHeaders({"authorization": f"Bearer {STATIC_KEY}"})
        assert extract_caller_key(h) == STATIC_KEY
        h = _FakeHeaders({"x-api-key": STATIC_KEY})
        assert extract_caller_key(h) == STATIC_KEY
        h = _FakeHeaders({"authorization": "Basic abc"})
        assert extract_caller_key(h) == ""

    def test_no_key_401(self, client):
        resp = client.get("/api/workflows")
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "unauthorized"

    def test_bearer_passes(self, client):
        resp = client.get("/api/workflows", headers={"Authorization": f"Bearer {STATIC_KEY}"})
        assert resp.status_code == 200

    def test_x_api_key_passes(self, client):
        resp = client.get("/api/nodes", headers={"x-api-key": STATIC_KEY})
        assert resp.status_code == 200
        assert resp.json()["count"] > 0

    def test_wrong_key_401(self, client):
        resp = client.get("/api/workflows", headers={"Authorization": "Bearer fc-wrong"})
        assert resp.status_code == 401

    def test_health_exempt(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


class TestWorkflowCrud:
    def _hdr(self):
        return {"Authorization": f"Bearer {STATIC_KEY}"}

    def test_create_get_list_delete(self, client):
        wf_def = {
            "name": "测试流",
            "description": "crud 用例",
            "workflow": {
                "id": "wf_crud",
                "name": "测试流",
                "nodes": [],
                "edges": [],
            },
        }
        created = client.post("/api/workflows", json=wf_def, headers=self._hdr())
        assert created.status_code == 201
        wf_id = created.json()["id"]

        got = client.get(f"/api/workflows/{wf_id}", headers=self._hdr())
        assert got.status_code == 200
        assert got.json()["workflow"]["name"] == "测试流"

        listed = client.get("/api/workflows", headers=self._hdr())
        assert listed.status_code == 200
        assert any(w["id"] == wf_id for w in listed.json()["workflows"])

        updated = client.put(
            f"/api/workflows/{wf_id}", json={"name": "改名", "workflow": wf_def["workflow"]}, headers=self._hdr()
        )
        assert updated.status_code == 200
        assert updated.json()["name"] == "改名"

        deleted = client.delete(f"/api/workflows/{wf_id}", headers=self._hdr())
        assert deleted.status_code == 200
        assert client.get(f"/api/workflows/{wf_id}", headers=self._hdr()).status_code == 404

    def test_get_missing_404(self, client):
        resp = client.get("/api/workflows/wf_missing", headers=self._hdr())
        assert resp.status_code == 404
        assert resp.json()["detail"]["code"] == "not_found"

    def test_invalid_id_rejected(self, client):
        resp = client.post(
            "/api/workflows",
            json={"id": "../evil", "name": "x", "workflow": {}},
            headers=self._hdr(),
        )
        assert resp.status_code in (400, 500)


class TestRunLifecycle:
    def _hdr(self):
        return {"Authorization": f"Bearer {STATIC_KEY}"}

    def _wf_def(self):
        return {
            "id": "wf_run",
            "name": "运行用例",
            "nodes": [
                {"id": "n1", "name": "mock_web_node", "config": {"params": {}}},
            ],
            "edges": [],
        }

    def test_run_to_completion(self, client):
        started = client.post("/api/runs", json={"workflow": self._wf_def(), "name": "运行用例"}, headers=self._hdr())
        assert started.status_code == 202
        run_id = started.json()["run_id"]
        assert started.json()["status"] == "running"

        status = None
        for _ in range(50):
            detail = client.get(f"/api/runs/{run_id}", headers=self._hdr())
            assert detail.status_code == 200
            status = detail.json()["status"]
            if status != "running":
                break
            import time

            time.sleep(0.1)
        assert status == "success", detail.json()
        assert any("hello-94" in json.dumps(s.get("output_data", {})) for s in detail.json()["steps"])

    def test_run_by_workflow_id(self, client):
        created = client.post(
            "/api/workflows",
            json={"id": "wf_run_ref", "name": "引用运行", "workflow": self._wf_def()},
            headers=self._hdr(),
        )
        assert created.status_code == 201
        started = client.post("/api/runs", json={"workflow_id": "wf_run_ref"}, headers=self._hdr())
        assert started.status_code == 202

    def test_runs_list(self, client):
        listed = client.get("/api/runs", headers=self._hdr())
        assert listed.status_code == 200
        assert "runs" in listed.json()

    def test_run_missing_workflow_404(self, client):
        resp = client.post("/api/runs", json={"workflow_id": "wf_missing"}, headers=self._hdr())
        assert resp.status_code == 404

    def test_run_empty_body_400(self, client):
        resp = client.post("/api/runs", json={}, headers=self._hdr())
        assert resp.status_code == 400

    def test_cancel_unknown_404(self, client):
        resp = client.post("/api/runs/run_missing/cancel", headers=self._hdr())
        assert resp.status_code == 404


class TestManifestAndDist:
    def test_manifest_fields_valid(self):
        manifest_path = Path(__file__).resolve().parent.parent / "web" / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["id"] == "fusion-cowork"
        assert manifest["entry"] == "/shell/fusion-cowork/"
        assert manifest["backend"] == "http://127.0.0.1:11432/shell/fusion-cowork/"
        assert manifest["health"] == "http://127.0.0.1:11432/shell/fusion-cowork/health"
        assert manifest["category"] in ("platform", "domain", "infra", "dev")
        assert "desktop" in manifest["devices"]
        assert "mobile-readonly" in manifest["devices"]
        assert manifest["title"] and manifest["title_en"]
        assert manifest["icon"]

    def test_vendored_packages_pinned(self):
        vendor = Path(__file__).resolve().parent.parent / "web" / "vendor" / "@fusion"
        for pkg in ("web-ui", "web-sdk"):
            pkg_json = vendor / pkg / "package.json"
            assert pkg_json.exists(), f"vendored {pkg} missing"
            assert (vendor / pkg / "src" / "index.ts").exists()
        root_pkg = Path(__file__).resolve().parent.parent / "web" / "package.json"
        assert "workspace:*" not in root_pkg.read_text(encoding="utf-8")

    def test_root_serves_spa(self, tmp_path, static_key_env):
        dist = Path(__file__).resolve().parent.parent / "web" / "dist"
        if not (dist / "index.html").exists():
            pytest.skip("web/dist not built — run `bun install && bun run build` in web/")
        from fastapi.testclient import TestClient

        from fusion_cowork.engine import WorkflowEngine
        from fusion_cowork.server.web_api import create_web_app

        app = create_web_app(engine=WorkflowEngine(), dist_dir=dist, store_root=tmp_path / "workflows")
        client = TestClient(app)
        resp = client.get("/", headers={"Authorization": f"Bearer {STATIC_KEY}"})
        assert resp.status_code == 200
        assert 'id="root"' in resp.text
        api_resp = client.get("/api/workflows", headers={"Authorization": f"Bearer {STATIC_KEY}"})
        assert api_resp.status_code == 200
