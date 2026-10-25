"""issue #146 + #147: web serve 鉴权契约修复 + 模型列表 stale 缓存。

#146 Contract:
- FUSION_COWORK_WEB_API_KEY 未配置时: 所有 /api/* 放行 (对齐 cli.py "未配置放行"
  与 README "unset = allow"), 不再无条件 401 (旧 bug 导致 shell 登录弹回循环)。
- 配置密钥时: 无/错 key 401, 正确 key 200 (现状保持)。

#147 Contract:
- /api/models 首次上游失败 (无缓存) → 200 + unavailable=True 空列表 envelope
  (issue #172: 冷缓存不再 503, 前端轮询拿到成功响应即不重试)。
- 上游成功过一次后再失败 → 200 + stale=True + 缓存列表, 消除前端重试风暴;
  chat 过滤在 stale 路径同样生效 (?all=1 返回原始缓存)。
"""

from __future__ import annotations

import pytest

STATIC_KEY = "fc-web-test-146"


@pytest.fixture
def store_root(tmp_path):
    return tmp_path / "workflows"


def _build_client(tmp_path, store_root, with_key: bool, monkeypatch):
    from fastapi.testclient import TestClient

    from fusion_cowork.engine import WorkflowEngine
    from fusion_cowork.server import web_api

    monkey_target = web_api
    monkey_target._WEB_DIR = tmp_path / "web"
    # issue #168: 本机 ~/.fusion-cowork/web_module_key 会激活鉴权 — 测试隔离, 回归纯 env 契约
    monkeypatch.setattr(web_api, "_module_key", lambda: "")
    if with_key:
        monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", STATIC_KEY)
    else:
        monkeypatch.delenv("FUSION_COWORK_WEB_API_KEY", raising=False)
    engine = WorkflowEngine()
    app = web_api.create_web_app(engine=engine, dist_dir=tmp_path / "no-dist", store_root=store_root)
    return TestClient(app)


class TestIssue146AuthContract:
    def test_unset_key_allows_api(self, tmp_path, store_root, monkeypatch):
        client = _build_client(tmp_path, store_root, with_key=False, monkeypatch=monkeypatch)
        resp = client.get("/api/nodes")
        assert resp.status_code == 200, f"未配置密钥应放行, 实得 {resp.status_code}: {resp.text}"

    def test_unset_key_allows_post(self, tmp_path, store_root, monkeypatch):
        client = _build_client(tmp_path, store_root, with_key=False, monkeypatch=monkeypatch)
        resp = client.post("/api/workflows", json={"name": "w", "workflow": {"nodes": [], "edges": []}})
        assert resp.status_code == 201

    def test_set_key_missing_rejected(self, tmp_path, store_root, monkeypatch):
        client = _build_client(tmp_path, store_root, with_key=True, monkeypatch=monkeypatch)
        assert client.get("/api/nodes").status_code == 401

    def test_set_key_wrong_rejected(self, tmp_path, store_root, monkeypatch):
        client = _build_client(tmp_path, store_root, with_key=True, monkeypatch=monkeypatch)
        resp = client.get("/api/nodes", headers={"x-api-key": "wrong-key"})
        assert resp.status_code == 401

    def test_set_key_correct_accepted(self, tmp_path, store_root, monkeypatch):
        client = _build_client(tmp_path, store_root, with_key=True, monkeypatch=monkeypatch)
        resp = client.get("/api/nodes", headers={"x-api-key": STATIC_KEY})
        assert resp.status_code == 200

    def test_health_always_open(self, tmp_path, store_root, monkeypatch):
        client = _build_client(tmp_path, store_root, with_key=True, monkeypatch=monkeypatch)
        assert client.get("/health").status_code == 200


class _FlakyMlx:
    """list_models 可编程失败的 MLX 客户端桩。"""

    def __init__(self, fail: bool):
        self.fail = fail
        self.calls = 0

    async def list_models(self):
        self.calls += 1
        if self.fail:
            raise RuntimeError("fusion-mlx 不在线")
        return [
            {"id": "chat-a", "engine_type": "text_generation"},
            {"id": "embed-b", "engine_type": "embedding"},
        ]


def _build_models_client(tmp_path, store_root, monkeypatch, mlx_stub):
    from types import SimpleNamespace

    from fastapi.testclient import TestClient

    from fusion_cowork.engine import WorkflowEngine
    from fusion_cowork.server import web_api

    web_api._WEB_DIR = tmp_path / "web"
    monkeypatch.delenv("FUSION_COWORK_WEB_API_KEY", raising=False)
    # issue #168: 本机 ~/.fusion-cowork/web_module_key 会激活鉴权 — 测试隔离
    monkeypatch.setattr(web_api, "_module_key", lambda: "")

    engine = WorkflowEngine()
    app = web_api.create_web_app(
        engine=engine, dist_dir=tmp_path / "no-dist", store_root=store_root, nl_generator=SimpleNamespace(mlx=mlx_stub)
    )
    return TestClient(app)


class TestIssue147ModelsStaleCache:
    def test_first_failure_no_cache_returns_unavailable_envelope(self, tmp_path, store_root, monkeypatch):
        client = _build_models_client(tmp_path, store_root, monkeypatch, _FlakyMlx(fail=True))
        resp = client.get("/api/models")
        assert resp.status_code == 200, "冷缓存上游失败应返回 200 envelope (issue #172), 而非 503"
        body = resp.json()
        assert body["models"] == []
        assert body["unavailable"] is True
        assert body["stale"] is False

    def test_success_then_failure_returns_stale_cache(self, tmp_path, store_root, monkeypatch):
        mlx = _FlakyMlx(fail=False)
        client = _build_models_client(tmp_path, store_root, monkeypatch, mlx)
        first = client.get("/api/models")
        assert first.status_code == 200
        assert first.json()["stale"] is False
        mlx.fail = True
        second = client.get("/api/models")
        assert second.status_code == 200, "有缓存时上游失败应返回 200 stale, 而非 503"
        body = second.json()
        assert body["stale"] is True
        assert body["count"] == first.json()["count"]

    def test_stale_path_keeps_chat_filter(self, tmp_path, store_root, monkeypatch):
        mlx = _FlakyMlx(fail=False)
        client = _build_models_client(tmp_path, store_root, monkeypatch, mlx)
        client.get("/api/models")
        mlx.fail = True
        body = client.get("/api/models").json()
        ids = [m["id"] for m in body["models"]]
        assert "embed-b" not in ids
        assert "chat-a" in ids

    def test_all_param_on_stale_returns_raw(self, tmp_path, store_root, monkeypatch):
        mlx = _FlakyMlx(fail=False)
        client = _build_models_client(tmp_path, store_root, monkeypatch, mlx)
        client.get("/api/models")
        mlx.fail = True
        body = client.get("/api/models?all=1").json()
        ids = [m["id"] for m in body["models"]]
        assert "embed-b" in ids and "chat-a" in ids
        assert body["stale"] is True

    def test_recovery_refreshes_cache(self, tmp_path, store_root, monkeypatch):
        mlx = _FlakyMlx(fail=False)
        client = _build_models_client(tmp_path, store_root, monkeypatch, mlx)
        client.get("/api/models")
        mlx.fail = True
        assert client.get("/api/models").json()["stale"] is True
        mlx.fail = False
        body = client.get("/api/models").json()
        assert body["stale"] is False
