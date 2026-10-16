"""P0 web 迁移批次 (migration doc §6): 模板目录 + 调度任务 REST。

Contract:
- GET /api/templates 列表 (内置模板非空), /api/templates/{id} 详情含 workflow 体,
  未知 id 404。
- POST /api/scheduler/tasks: cron 与 interval 两条创建路径; 缺参 400; 双触发器 400;
  未知工作流 404; 非法 cron 400。list/pause/resume/delete 全生命周期。
- scheduler 经 create_web_app(scheduler=...) 注入并隔离 task_store_path
  (issue #98 同类: 不允许落 home 目录)。
"""

from __future__ import annotations

import pytest

STATIC_KEY = "fc-web-test-p0"


@pytest.fixture
def static_key_env(monkeypatch):
    monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", STATIC_KEY)
    return STATIC_KEY


@pytest.fixture
def store_root(tmp_path):
    return tmp_path / "workflows"


@pytest.fixture
def scheduler(tmp_path):
    from fusion_cowork.engine.scheduler import TaskScheduler

    return TaskScheduler(task_store_path=str(tmp_path / "sched.json"))


@pytest.fixture
def client(static_key_env, store_root, tmp_path, scheduler):
    from fastapi.testclient import TestClient

    from fusion_cowork.engine import WorkflowEngine
    from fusion_cowork.engine.node import BaseNode, NodeCategory, NodeRegistry, NodeResult, NodeStatus, register_node
    from fusion_cowork.server import web_api

    saved = dict(NodeRegistry._registry)
    try:
        monkey_target = web_api
        monkey_target._WEB_DIR = tmp_path / "web"

        @register_node
        class MockP0Node(BaseNode):
            name = "mock_p0_node"
            category = NodeCategory.TOOL

            async def execute(self, params):
                return NodeResult(status=NodeStatus.SUCCESS, data={"ok": True}, summary="p0 ok")

        engine = WorkflowEngine()
        app = web_api.create_web_app(
            engine=engine,
            dist_dir=tmp_path / "no-dist",
            store_root=store_root,
            scheduler=scheduler,
        )
        yield TestClient(app)
    finally:
        NodeRegistry._registry.clear()
        NodeRegistry._registry.update(saved)


def _auth(client):
    return {"Authorization": f"Bearer {STATIC_KEY}"}


def _make_workflow(client):
    resp = client.post(
        "/api/workflows",
        headers=_auth(client),
        json={"name": "p0-wf", "description": "", "workflow": {"nodes": [], "edges": []}},
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


class TestTemplates:
    def test_list_nonempty(self, client):
        resp = client.get("/api/templates", headers=_auth(client))
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] > 0
        assert all("id" in t and "name" in t for t in body["templates"])

    def test_detail_has_workflow_body(self, client):
        listed = client.get("/api/templates", headers=_auth(client)).json()["templates"]
        tpl_id = listed[0]["id"]
        resp = client.get(f"/api/templates/{tpl_id}", headers=_auth(client))
        assert resp.status_code == 200
        assert "workflow" in resp.json()

    def test_detail_404(self, client):
        resp = client.get("/api/templates/no-such-tpl", headers=_auth(client))
        assert resp.status_code == 404

    def test_unauthorized(self, client):
        assert client.get("/api/templates").status_code == 401


class TestSchedulerTasks:
    def test_create_cron_task(self, client, scheduler):
        wf_id = _make_workflow(client)
        resp = client.post(
            "/api/scheduler/tasks",
            headers=_auth(client),
            json={"name": "daily-p0", "workflow_id": wf_id, "cron": "0 9 * * *"},
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["workflow_id"] == wf_id
        assert body["status"] == "active"
        assert scheduler.get_task(body["id"]) is not None

    def test_create_interval_task(self, client):
        wf_id = _make_workflow(client)
        resp = client.post(
            "/api/scheduler/tasks",
            headers=_auth(client),
            json={"name": "tick-p0", "workflow_id": wf_id, "interval_minutes": 30},
        )
        assert resp.status_code == 201, resp.text
        listed = client.get("/api/scheduler/tasks", headers=_auth(client)).json()
        tick = next(t for t in listed["tasks"] if t["name"] == "tick-p0")
        assert tick["trigger_type"] != "cron"
        assert tick["workflow_id"] == wf_id

    def test_create_missing_fields_400(self, client):
        resp = client.post("/api/scheduler/tasks", headers=_auth(client), json={"name": "x"})
        assert resp.status_code == 400

    def test_create_unknown_workflow_404(self, client):
        resp = client.post(
            "/api/scheduler/tasks",
            headers=_auth(client),
            json={"name": "x", "workflow_id": "no-such-wf", "cron": "0 9 * * *"},
        )
        assert resp.status_code == 404

    def test_create_double_trigger_400(self, client):
        wf_id = _make_workflow(client)
        resp = client.post(
            "/api/scheduler/tasks",
            headers=_auth(client),
            json={"name": "x", "workflow_id": wf_id, "cron": "0 9 * * *", "interval_minutes": 5},
        )
        assert resp.status_code == 400

    def test_create_no_trigger_400(self, client):
        wf_id = _make_workflow(client)
        resp = client.post("/api/scheduler/tasks", headers=_auth(client), json={"name": "x", "workflow_id": wf_id})
        assert resp.status_code == 400

    def test_create_invalid_cron_400(self, client):
        wf_id = _make_workflow(client)
        resp = client.post(
            "/api/scheduler/tasks",
            headers=_auth(client),
            json={"name": "x", "workflow_id": wf_id, "cron": "not a cron"},
        )
        assert resp.status_code == 400

    def test_pause_resume_delete_lifecycle(self, client):
        wf_id = _make_workflow(client)
        task_id = client.post(
            "/api/scheduler/tasks",
            headers=_auth(client),
            json={"name": "life-p0", "workflow_id": wf_id, "cron": "0 9 * * 1"},
        ).json()["id"]

        assert client.post(f"/api/scheduler/tasks/{task_id}/pause", headers=_auth(client)).json()["status"] == "paused"
        listed = client.get("/api/scheduler/tasks", headers=_auth(client)).json()
        assert next(t for t in listed["tasks"] if t["id"] == task_id)["status"] == "paused"

        assert client.post(f"/api/scheduler/tasks/{task_id}/resume", headers=_auth(client)).json()["status"] == "active"

        assert client.delete(f"/api/scheduler/tasks/{task_id}", headers=_auth(client)).status_code == 200
        ids = [t["id"] for t in client.get("/api/scheduler/tasks", headers=_auth(client)).json()["tasks"]]
        assert task_id not in ids

    def test_pause_unknown_404(self, client):
        assert client.post("/api/scheduler/tasks/no-such/pause", headers=_auth(client)).status_code == 404

    def test_resume_unknown_404(self, client):
        assert client.post("/api/scheduler/tasks/no-such/resume", headers=_auth(client)).status_code == 404

    def test_delete_unknown_404(self, client):
        assert client.delete("/api/scheduler/tasks/no-such", headers=_auth(client)).status_code == 404

    def test_task_store_isolated(self, scheduler, tmp_path):
        assert scheduler._task_store_path == str(tmp_path / "sched.json")
