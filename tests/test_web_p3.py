"""P3-1 web 迁移批次 (migration doc §6): W17 模型目录 + W20 /rpc 端点。

Contract:
- GET /api/models: nl_generator.mlx.list_models() 透传; 无 mlx 或上游失败 503。
- POST /api/ai/generate-workflow: 可选 model 覆盖 (临时生成器, 不污染共享实例)。
- POST /rpc: rpc_bridge dispatch — 非 plugins 方法 -32601; plugins/bogus
  依赖在 -32601 / 缺 -32603; JSON 解析失败 400。
"""

from __future__ import annotations

import pytest

STATIC_KEY = "fc-web-test-p3"


class FakeMlx:
    def __init__(self, fail: bool = False):
        self.fail = fail

    async def list_models(self):
        if self.fail:
            raise RuntimeError("mlx offline")
        return [{"id": "qwen-chat-8b", "engine_type": "chat"}, {"id": "flux-img", "engine_type": "image_gen"}]


class FakeGenerator:
    def __init__(self, mlx=None, fail: bool = False):
        self.mlx = mlx or FakeMlx(fail=fail)


@pytest.fixture
def client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from fusion_cowork.server import web_api

    monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", STATIC_KEY)
    web_api._WEB_DIR = tmp_path / "web"
    app = web_api.create_web_app(
        dist_dir=tmp_path / "no-dist",
        store_root=tmp_path / "workflows",
        nl_generator=FakeGenerator(),
    )
    return TestClient(app)


def _auth():
    return {"Authorization": f"Bearer {STATIC_KEY}"}


class TestModelsCatalog:
    def test_list_models_ok(self, client):
        resp = client.get("/api/models", headers=_auth())
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 2
        assert {m["id"] for m in body["models"]} == {"qwen-chat-8b", "flux-img"}

    def test_list_models_upstream_fail(self, monkeypatch, tmp_path):
        from fastapi.testclient import TestClient

        from fusion_cowork.server import web_api

        monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", STATIC_KEY)
        web_api._WEB_DIR = tmp_path / "web"
        app = web_api.create_web_app(
            dist_dir=tmp_path / "no-dist",
            store_root=tmp_path / "workflows",
            nl_generator=FakeGenerator(FakeMlx(fail=True)),
        )
        resp = TestClient(app).get("/api/models", headers=_auth())
        assert resp.status_code == 503

    def test_generate_with_model_override(self, client):
        resp = client.post(
            "/api/ai/generate-workflow",
            headers=_auth(),
            json={"prompt": "整理下载", "model": "qwen-chat-8b"},
        )
        # FakeMlx 无 chat 能力 → 临时生成器走到推理失败 → 502 (证明 model 路径已接通)
        assert resp.status_code == 502

    def test_generate_requires_prompt(self, client):
        assert client.post("/api/ai/generate-workflow", headers=_auth(), json={"model": "x"}).status_code == 400


class TestRpcEndpoint:
    def test_rejects_non_plugins_method(self, client):
        resp = client.post("/rpc", headers=_auth(), json={"jsonrpc": "2.0", "id": 1, "method": "desk.nodes.list"})
        assert resp.status_code == 200
        assert resp.json()["error"]["code"] == -32601

    def test_plugins_unknown_method_degrades(self, client):
        from fusion_cowork.server.rpc_bridge import is_plugins_available

        resp = client.post("/rpc", headers=_auth(), json={"jsonrpc": "2.0", "id": 2, "method": "plugins/bogus"})
        body = resp.json()
        expected = -32601 if is_plugins_available() else -32603
        assert body["error"]["code"] == expected

    def test_parse_error(self, client):
        resp = client.post("/rpc", headers={**_auth(), "Content-Type": "application/json"}, content=b"not-json")
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == -32700
