"""W8 运行改向 (migration doc §6): 引擎按执行粒度暂停/恢复 + web 路由。

Contract:
- WorkflowEngine.pause/resume/is_paused: 执行暂停在下一节点检查点, 恢复后
  继续完成; cancel 唤醒暂停中的执行。
- POST /api/runs/{id}/pause|resume: 409 非运行态/非暂停态; 暂停后记录
  status=paused, 恢复回 running。
- 追加指令 (append-instruction) 属 orchestrator 交互协议 (I2), 本批不含。
"""

from __future__ import annotations

import asyncio

import pytest

STATIC_KEY = "fc-web-test-p5"


class P5Node:
    name = "p5_node"
    display_name = "P5 暂停测试节点"
    category = None
    description = "测试用"
    icon = "🧪"
    default_label = "p5"


def _make_node_class(name: str, delay: float):
    from fusion_cowork.engine.node import BaseNode, NodeCategory, NodeResult, NodeStatus

    class _Node(BaseNode):
        delay_sec = delay

        async def execute(self, inputs):
            await asyncio.sleep(self.delay_sec)
            return NodeResult(status=NodeStatus.SUCCESS, data={"ok": True}, summary="ok")

    _Node.name = name
    _Node.display_name = name
    _Node.category = NodeCategory.TOOL
    _Node.description = "测试用"
    _Node.icon = "🧪"
    _Node.default_label = name
    return _Node


@pytest.fixture
def client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from fusion_cowork.server import web_api

    monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", STATIC_KEY)
    web_api._WEB_DIR = tmp_path / "web"
    app = web_api.create_web_app(
        dist_dir=tmp_path / "no-dist",
        store_root=tmp_path / "workflows",
        space_data_dir=tmp_path / "spaces",
    )
    yield TestClient(app), app


def _auth():
    return {"Authorization": f"Bearer {STATIC_KEY}"}


class TestEnginePauseResume:
    async def test_pause_holds_between_nodes_then_resume_completes(self):
        from fusion_cowork.engine.node import NodeRegistry
        from fusion_cowork.engine.workflow import Workflow, WorkflowEngine

        registry_saved = dict(NodeRegistry._registry)
        first = _make_node_class("p5_first", 0.02)
        second = _make_node_class("p5_second", 0.02)
        try:
            NodeRegistry.register(first)
            NodeRegistry.register(second)
            wf = Workflow.from_dict(
                {
                    "id": "wf-p5",
                    "name": "p5",
                    "nodes": [
                        {"id": "n1", "name": "p5_first", "config": {"params": {}}},
                        {"id": "n2", "name": "p5_second", "config": {"params": {}}},
                    ],
                    "edges": [{"source_id": "n1", "target_id": "n2"}],
                }
            )
            engine = WorkflowEngine()
            task = asyncio.create_task(engine.execute(wf, execution_id="exec-p5"))
            await asyncio.sleep(0.01)
            assert engine.pause("exec-p5") is True
            # 等 n1 完成 (0.02s) + 余量 — 暂停挂起在 n2 检查点
            await asyncio.sleep(0.1)
            execution = engine.get_execution("exec-p5")
            done_nodes = [s.node_id for s in execution.steps if s.status.value == "success"]
            assert done_nodes == ["n1"]
            assert engine.is_paused("exec-p5") is True
            engine.resume("exec-p5")
            execution = await asyncio.wait_for(task, timeout=5)
            assert execution.status.value == "success"
            assert len(execution.steps) == 2
            assert engine.is_paused("exec-p5") is False
        finally:
            NodeRegistry._registry.clear()
            NodeRegistry._registry.update(registry_saved)

    async def test_cancel_wakes_paused_execution(self):
        from fusion_cowork.engine.node import NodeRegistry
        from fusion_cowork.engine.workflow import Workflow, WorkflowEngine

        registry_saved = dict(NodeRegistry._registry)
        node_cls = _make_node_class("p5_cancel_node", 0.02)
        try:
            NodeRegistry.register(node_cls)
            wf = Workflow.from_dict(
                {
                    "id": "wf-p5c",
                    "name": "p5c",
                    "nodes": [
                        {"id": "a", "name": "p5_cancel_node", "config": {"params": {}}},
                        {"id": "b", "name": "p5_cancel_node", "config": {"params": {}}},
                    ],
                    "edges": [{"source_id": "a", "target_id": "b"}],
                }
            )
            engine = WorkflowEngine()
            task = asyncio.create_task(engine.execute(wf, execution_id="exec-p5c"))
            await asyncio.sleep(0.01)
            assert engine.pause("exec-p5c") is True
            await asyncio.sleep(0.1)
            assert engine.is_paused("exec-p5c") is True
            engine.cancel("exec-p5c")
            execution = await asyncio.wait_for(task, timeout=2)
            assert execution.status.value == "cancelled"
        finally:
            NodeRegistry._registry.clear()
            NodeRegistry._registry.update(registry_saved)

    def test_pause_unknown_execution(self):
        from fusion_cowork.engine.workflow import WorkflowEngine

        engine = WorkflowEngine()
        assert engine.pause("nope") is False
        assert engine.resume("nope") is False
        assert engine.is_paused("nope") is False


class TestRunPauseRoutes:
    def test_pause_resume_unknown_run(self, client):
        tc, _ = client
        assert tc.post("/api/runs/run_none/pause", headers=_auth()).status_code == 409
        assert tc.post("/api/runs/run_none/resume", headers=_auth()).status_code == 409

    def _seed_run(self, registry, run_id, status):
        registry._runs[run_id] = {
            "run_id": run_id,
            "workflow_id": "wf-1",
            "workflow_name": "wf",
            "status": status,
            "created_at": 1.0,
            "finished_at": None,
            "steps": [],
            "result": None,
            "error": None,
        }

    def test_pause_resume_lifecycle(self, client):
        tc, app = client
        registry = app.state.run_registry
        self._seed_run(registry, "run_p5", "running")
        registry._engine.pause = lambda rid: True
        registry._engine.resume = lambda rid: True
        r = tc.post("/api/runs/run_p5/pause", headers=_auth())
        assert r.status_code == 200
        assert r.json()["status"] == "paused"
        assert registry._runs["run_p5"]["status"] == "paused"
        assert tc.post("/api/runs/run_p5/pause", headers=_auth()).status_code == 409
        r2 = tc.post("/api/runs/run_p5/resume", headers=_auth())
        assert r2.status_code == 200
        assert registry._runs["run_p5"]["status"] == "running"
        assert tc.post("/api/runs/run_p5/resume", headers=_auth()).status_code == 409

    def test_cancel_allowed_from_paused(self, client):
        tc, app = client
        registry = app.state.run_registry
        self._seed_run(registry, "run_p6", "paused")
        registry._engine.cancel = lambda rid: True
        r = tc.post("/api/runs/run_p6/cancel", headers=_auth())
        assert r.status_code == 200
