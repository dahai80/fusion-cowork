"""P1 web 迁移批次 (migration doc §6): NL 委托 + 会话列表。

Contract:
- POST /api/ai/generate-workflow: prompt 必填 400; 注入生成器 200 含 workflow;
  生成器异常 502; 空 nodes 502。
- GET /api/sessions + /{id}: SessionStore 注入隔离 (tmp db), 列表/详情/404。
"""

from __future__ import annotations

import time

import pytest

STATIC_KEY = "fc-web-test-p1"


class FakeGenerator:
    def __init__(self, fail=False, empty=False):
        self.fail = fail
        self.empty = empty

    async def generate(self, prompt, context=None):
        if self.fail:
            raise RuntimeError("mlx down")
        if self.empty:
            return {"name": "x", "nodes": []}
        return {
            "name": "gen-wf",
            "description": "generated draft",
            "nodes": [
                {"id": "n1", "name": "mock_p1_node", "category": "tool", "config": {"params": {}}},
                {"id": "n2", "name": "file_input", "category": "io"},
            ],
            "edges": [{"source_id": "n1", "target_id": "n2"}],
        }


@pytest.fixture
def static_key_env(monkeypatch):
    monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", STATIC_KEY)
    return STATIC_KEY


@pytest.fixture
def session_store(tmp_path):
    from fusion_cowork.engine.session import SessionStore

    return SessionStore(db_path=str(tmp_path / "sessions.db"))


def _auth(client):
    return {"Authorization": f"Bearer {STATIC_KEY}"}


def _build_client(tmp_path, session_store, generator):
    from fastapi.testclient import TestClient

    from fusion_cowork.engine import WorkflowEngine
    from fusion_cowork.engine.node import BaseNode, NodeCategory, NodeRegistry, NodeResult, NodeStatus, register_node
    from fusion_cowork.server import web_api

    saved = dict(NodeRegistry._registry)
    try:
        web_api._WEB_DIR = tmp_path / "web"

        @register_node
        class MockP1Node(BaseNode):
            name = "mock_p1_node"
            category = NodeCategory.TOOL

            async def execute(self, params):
                return NodeResult(status=NodeStatus.SUCCESS, data={"p1": True}, summary="p1 ok")

        engine = WorkflowEngine()
        app = web_api.create_web_app(
            engine=engine,
            dist_dir=tmp_path / "no-dist",
            store_root=tmp_path / "workflows",
            nl_generator=generator,
            session_store=session_store,
        )
        return TestClient(app)
    finally:
        NodeRegistry._registry.clear()
        NodeRegistry._registry.update(saved)


@pytest.fixture
def client(static_key_env, tmp_path, session_store):
    return _build_client(tmp_path, session_store, FakeGenerator())


@pytest.fixture
def fail_client(static_key_env, tmp_path, session_store):
    return _build_client(tmp_path, session_store, FakeGenerator(fail=True))


@pytest.fixture
def empty_client(static_key_env, tmp_path, session_store):
    return _build_client(tmp_path, session_store, FakeGenerator(empty=True))


class TestNLGenerate:
    def test_generate_200(self, client):
        resp = client.post(
            "/api/ai/generate-workflow",
            headers=_auth(client),
            json={"prompt": "tidy downloads and report"},
        )
        assert resp.status_code == 200
        assert resp.json()["workflow"]["name"] == "gen-wf"

    def test_missing_prompt_400(self, client):
        resp = client.post("/api/ai/generate-workflow", headers=_auth(client), json={})
        assert resp.status_code == 400

    def test_unauthorized(self, client):
        resp = client.post("/api/ai/generate-workflow", json={"prompt": "x"})
        assert resp.status_code == 401

    def test_generator_error_502(self, fail_client):
        resp = fail_client.post("/api/ai/generate-workflow", headers=_auth(fail_client), json={"prompt": "x"})
        assert resp.status_code == 502

    def test_empty_nodes_502(self, empty_client):
        resp = empty_client.post("/api/ai/generate-workflow", headers=_auth(empty_client), json={"prompt": "x"})
        assert resp.status_code == 502


class TestSessions:
    def test_list_empty(self, client):
        resp = client.get("/api/sessions", headers=_auth(client))
        assert resp.status_code == 200
        assert resp.json()["count"] == 0

    def test_list_after_save(self, client, session_store):
        from fusion_cowork.engine.session import Session

        session_store.save(Session(workflow_name="wf-a", status="running", initial_input={"q": 1}))
        session_store.save(Session(workflow_name="wf-b", status="completed", completed_at=time.time()))
        resp = client.get("/api/sessions", headers=_auth(client))
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 2
        assert {s["workflow_name"] for s in body["sessions"]} == {"wf-a", "wf-b"}
        assert all("steps_snapshot" not in s for s in body["sessions"])

    def test_detail_200_and_404(self, client, session_store):
        from fusion_cowork.engine.session import Session

        s = session_store.save(Session(workflow_name="wf-c", status="running"))
        resp = client.get(f"/api/sessions/{s.id}", headers=_auth(client))
        assert resp.status_code == 200
        assert resp.json()["workflow_name"] == "wf-c"
        assert client.get("/api/sessions/no-such", headers=_auth(client)).status_code == 404

    def test_unauthorized(self, client):
        assert client.get("/api/sessions").status_code == 401
