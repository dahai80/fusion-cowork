"""#113 W8/B1: 运行改向指令注入协议。

Contract:
- WorkflowEngine.add_directive: set_params / insert_node / skip_remaining 在
  下一节点边界生效; 未知 action 或未知执行返回 False。
- 改向指令落轨迹 (TrajectoryRecorder.record_directive, event=directive)。
- POST /api/runs/{id}/directive: 202 已排队, 400 非法 action, 409 非运行态。
"""

from __future__ import annotations

import asyncio
import json

import pytest

STATIC_KEY = "fc-web-test-113"


def _make_value_node(name: str, delay: float = 0.02):
    from fusion_cowork.engine.node import BaseNode, NodeCategory, NodeResult, NodeStatus

    class _Node(BaseNode):
        delay_sec = delay

        async def execute(self, inputs):
            await asyncio.sleep(self.delay_sec)
            return NodeResult(
                status=NodeStatus.SUCCESS,
                data={"value": self.config.params.get("value")},
                summary=f"value={self.config.params.get('value')}",
            )

    _Node.name = name
    _Node.display_name = name
    _Node.category = NodeCategory.TOOL
    _Node.description = "测试用"
    _Node.icon = "🧪"
    _Node.default_label = name
    return _Node


def _workflow(nodes, edges, wf_id="wf-113"):
    from fusion_cowork.engine.workflow import Workflow

    return Workflow.from_dict(
        {
            "id": wf_id,
            "name": wf_id,
            "nodes": nodes,
            "edges": edges,
        }
    )


@pytest.fixture
def two_node_workflow():
    from fusion_cowork.engine.node import NodeRegistry

    saved = dict(NodeRegistry._registry)
    first = _make_value_node("dir_first")
    second = _make_value_node("dir_second")
    NodeRegistry.register(first)
    NodeRegistry.register(second)
    try:
        yield _workflow(
            [
                {"id": "n1", "name": "dir_first", "config": {"params": {"value": "A"}}},
                {"id": "n2", "name": "dir_second", "config": {"params": {"value": "B"}}},
            ],
            [{"source_id": "n1", "target_id": "n2"}],
        )
    finally:
        NodeRegistry._registry.clear()
        NodeRegistry._registry.update(saved)


async def _pause_at_checkpoint(engine, exec_id):
    await asyncio.sleep(0.01)
    assert engine.pause(exec_id) is True
    await asyncio.sleep(0.1)
    assert engine.is_paused(exec_id) is True


class TestEngineDirectives:
    async def test_set_params_overrides_next_node(self, two_node_workflow):
        from fusion_cowork.engine.workflow import WorkflowEngine

        engine = WorkflowEngine()
        task = asyncio.create_task(engine.execute(two_node_workflow, execution_id="exec-d1"))
        await _pause_at_checkpoint(engine, "exec-d1")

        assert engine.add_directive("exec-d1", {"action": "set_params", "node_id": "n2", "params": {"value": "X"}})
        engine.resume("exec-d1")
        execution = await asyncio.wait_for(task, timeout=5)

        assert execution.status.value == "success"
        assert execution.steps[1].output_data["value"] == "X"

    async def test_set_params_defaults_to_next_node(self, two_node_workflow):
        from fusion_cowork.engine.workflow import WorkflowEngine

        engine = WorkflowEngine()
        task = asyncio.create_task(engine.execute(two_node_workflow, execution_id="exec-d1b"))
        await _pause_at_checkpoint(engine, "exec-d1b")

        assert engine.add_directive("exec-d1b", {"action": "set_params", "params": {"value": "NEXT"}})
        engine.resume("exec-d1b")
        execution = await asyncio.wait_for(task, timeout=5)

        assert execution.steps[1].output_data["value"] == "NEXT"

    async def test_insert_node_runs_in_order(self, two_node_workflow):
        from fusion_cowork.engine.node import NodeRegistry
        from fusion_cowork.engine.workflow import WorkflowEngine

        saved = dict(NodeRegistry._registry)
        NodeRegistry.register(_make_value_node("dir_inserted"))
        try:
            engine = WorkflowEngine()
            task = asyncio.create_task(engine.execute(two_node_workflow, execution_id="exec-d2"))
            await _pause_at_checkpoint(engine, "exec-d2")

            assert engine.add_directive(
                "exec-d2",
                {
                    "action": "insert_node",
                    "node": {"name": "dir_inserted", "params": {"value": "I"}},
                    "after_node_id": "n1",
                },
            )
            engine.resume("exec-d2")
            execution = await asyncio.wait_for(task, timeout=5)

            assert execution.status.value == "success"
            names = [s.node_name for s in execution.steps]
            assert names[0] == "dir_first"
            assert "dir_inserted" in names
            assert names[-1] == "dir_second"
            inserted = next(s for s in execution.steps if s.node_name == "dir_inserted")
            assert inserted.output_data["value"] == "I"
        finally:
            NodeRegistry._registry.clear()
            NodeRegistry._registry.update(saved)

    async def test_skip_remaining_marks_skipped(self, two_node_workflow):
        from fusion_cowork.engine.workflow import WorkflowEngine

        engine = WorkflowEngine()
        task = asyncio.create_task(engine.execute(two_node_workflow, execution_id="exec-d3"))
        await _pause_at_checkpoint(engine, "exec-d3")

        assert engine.add_directive("exec-d3", {"action": "skip_remaining"})
        engine.resume("exec-d3")
        execution = await asyncio.wait_for(task, timeout=5)

        assert execution.status.value == "success"
        assert [s.status.value for s in execution.steps] == ["success", "skipped"]
        assert execution.steps[1].summary == "运行改向: 跳过剩余节点"

    def test_invalid_directive_rejected(self):
        from fusion_cowork.engine.workflow import WorkflowEngine

        engine = WorkflowEngine()
        assert engine.add_directive("missing", {"action": "set_params", "params": {"a": 1}}) is False
        assert engine.add_directive("missing", {"action": "explode"}) is False


class TestDirectiveTrajectory:
    def test_record_directive_writes_jsonl(self, tmp_path):
        from fusion_cowork.engine.hooks import HookManager
        from fusion_cowork.trajectory.recorder import TrajectoryRecorder

        recorder = TrajectoryRecorder(HookManager(), trajectory_dir=str(tmp_path))
        recorder.bind_session("sess-113")
        recorder.record_directive("exec-113", {"action": "skip_remaining"})

        path = tmp_path / "sess-113.jsonl"
        assert path.exists()
        event = json.loads(path.read_text().strip().splitlines()[-1])
        assert event["event"] == "directive"
        assert event["execution_id"] == "exec-113"
        assert event["data"]["action"] == "skip_remaining"


class TestDirectiveRoute:
    @pytest.fixture
    def client(self, monkeypatch, tmp_path):
        from fastapi.testclient import TestClient

        from fusion_cowork.server import web_api

        monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", STATIC_KEY)
        web_api._WEB_DIR = tmp_path / "web"
        app = web_api.create_web_app(
            dist_dir=tmp_path / "no-dist",
            store_root=tmp_path / "workflows",
            space_data_dir=tmp_path / "spaces",
        )
        return TestClient(app), app

    def _seed(self, registry, run_id, status):
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

    def test_directive_accepts_and_queues(self, client):
        tc, app = client
        registry = app.state.run_registry
        self._seed(registry, "run_113", "paused")
        registry._engine.add_directive = lambda rid, d: True

        r = tc.post(
            "/api/runs/run_113/directive",
            json={"action": "set_params", "node_id": "n2", "params": {"x": 1}},
            headers={"Authorization": f"Bearer {STATIC_KEY}"},
        )
        assert r.status_code == 202
        assert r.json()["status"] == "queued"
        assert registry._runs["run_113"]["directives"][0]["action"] == "set_params"

    def test_directive_rejects_bad_action(self, client):
        tc, app = client
        registry = app.state.run_registry
        self._seed(registry, "run_114", "running")

        r = tc.post(
            "/api/runs/run_114/directive",
            json={"action": "explode"},
            headers={"Authorization": f"Bearer {STATIC_KEY}"},
        )
        assert r.status_code == 400

    def test_directive_rejects_finished_run(self, client):
        tc, app = client
        registry = app.state.run_registry
        self._seed(registry, "run_115", "completed")

        r = tc.post(
            "/api/runs/run_115/directive",
            json={"action": "skip_remaining"},
            headers={"Authorization": f"Bearer {STATIC_KEY}"},
        )
        assert r.status_code == 409
