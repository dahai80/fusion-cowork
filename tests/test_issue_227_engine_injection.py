"""#227 (D3): 引擎 pause/resume + 运行中指令注入。

Contract:
- inject 指令: 下一节点边界把 instruction 并入目标节点输入 (user_instruction 键),
  时间线落一条 user_correction 标记步 (skipped 零耗时, 非节点执行)。
- 空指令早拒; 未知执行/未知 action 拒绝。
- pause 语义 graceful: 当前节点完成后挂起, resume 续跑, 步骤齐全。
- POST /api/runs/{id}/directive inject 202; 空指令 409; _step_dict 透出 user_correction。
"""

from __future__ import annotations

import asyncio

import pytest

STATIC_KEY = "fc-web-test-227"


def _make_echo_node(name: str, delay: float = 0.02):
    from fusion_cowork.engine.node import BaseNode, NodeCategory, NodeResult, NodeStatus

    class _Node(BaseNode):
        delay_sec = delay

        async def execute(self, inputs):
            await asyncio.sleep(self.delay_sec)
            return NodeResult(
                status=NodeStatus.SUCCESS,
                data={"echo": dict(inputs)},
                summary=f"echo keys={sorted(inputs)}",
            )

    _Node.name = name
    _Node.display_name = name
    _Node.category = NodeCategory.TOOL
    _Node.description = "测试用"
    _Node.icon = "🧪"
    _Node.default_label = name
    return _Node


@pytest.fixture
def three_node_workflow():
    from fusion_cowork.engine.node import NodeRegistry
    from fusion_cowork.engine.workflow import Workflow

    saved = dict(NodeRegistry._registry)
    NodeRegistry.register(_make_echo_node("inj_first"))
    NodeRegistry.register(_make_echo_node("inj_second"))
    NodeRegistry.register(_make_echo_node("inj_third"))
    try:
        yield Workflow.from_dict(
            {
                "id": "wf-227",
                "name": "wf-227",
                "nodes": [
                    {"id": "n1", "name": "inj_first"},
                    {"id": "n2", "name": "inj_second"},
                    {"id": "n3", "name": "inj_third"},
                ],
                "edges": [
                    {"source_id": "n1", "target_id": "n2"},
                    {"source_id": "n2", "target_id": "n3"},
                ],
            }
        )
    finally:
        NodeRegistry._registry.clear()
        NodeRegistry._registry.update(saved)


async def _pause_after_first(engine, exec_id):
    await asyncio.sleep(0.01)
    assert engine.pause(exec_id) is True
    await asyncio.sleep(0.12)
    assert engine.is_paused(exec_id) is True


class TestEngineInject:
    async def test_inject_reaches_next_node_and_marks_timeline(self, three_node_workflow):
        from fusion_cowork.engine.workflow import WorkflowEngine

        engine = WorkflowEngine()
        task = asyncio.create_task(engine.execute(three_node_workflow, execution_id="exec-i1"))
        await _pause_after_first(engine, "exec-i1")

        assert engine.add_directive("exec-i1", {"action": "inject", "instruction": "skip travel category"})
        engine.resume("exec-i1")
        execution = await asyncio.wait_for(task, timeout=5)

        assert execution.status.value == "success"
        second = next(s for s in execution.steps if s.node_id == "n2" and not s.user_correction)
        assert second.input_data.get("user_instruction") == "skip travel category"
        markers = [s for s in execution.steps if s.user_correction]
        assert len(markers) == 1
        assert markers[0].node_id == "n2"
        assert "skip travel category" in markers[0].summary
        assert markers[0].status.value == "skipped"

    async def test_inject_explicit_target_and_merge(self, three_node_workflow):
        from fusion_cowork.engine.workflow import WorkflowEngine

        engine = WorkflowEngine()
        task = asyncio.create_task(engine.execute(three_node_workflow, execution_id="exec-i2"))
        await _pause_after_first(engine, "exec-i2")

        assert engine.add_directive(
            "exec-i2",
            {"action": "inject", "node_id": "n3", "instruction": "use plan B", "merge": {"budget": 42}},
        )
        engine.resume("exec-i2")
        execution = await asyncio.wait_for(task, timeout=5)

        assert execution.status.value == "success"
        third = next(s for s in execution.steps if s.node_id == "n3" and not s.user_correction)
        assert third.input_data.get("user_instruction") == "use plan B"
        assert third.input_data.get("budget") == 42
        # n2 未被点名 → 不吃指令
        second = next(s for s in execution.steps if s.node_id == "n2" and not s.user_correction)
        assert "user_instruction" not in second.input_data

    async def test_inject_empty_instruction_rejected(self, three_node_workflow):
        from fusion_cowork.engine.workflow import WorkflowEngine

        engine = WorkflowEngine()
        task = asyncio.create_task(engine.execute(three_node_workflow, execution_id="exec-i3"))
        await _pause_after_first(engine, "exec-i3")

        assert engine.add_directive("exec-i3", {"action": "inject", "instruction": "  "}) is False
        engine.resume("exec-i3")
        execution = await asyncio.wait_for(task, timeout=5)

        assert execution.status.value == "success"
        assert not any(s.user_correction for s in execution.steps)

    async def test_inject_unknown_target_ignored_run_survives(self, three_node_workflow):
        from fusion_cowork.engine.workflow import WorkflowEngine

        engine = WorkflowEngine()
        task = asyncio.create_task(engine.execute(three_node_workflow, execution_id="exec-i4"))
        await _pause_after_first(engine, "exec-i4")

        assert engine.add_directive("exec-i4", {"action": "inject", "node_id": "ghost", "instruction": "x"})
        engine.resume("exec-i4")
        execution = await asyncio.wait_for(task, timeout=5)

        assert execution.status.value == "success"
        assert not any(s.user_correction for s in execution.steps)

    def test_inject_unknown_execution_rejected(self):
        from fusion_cowork.engine.workflow import WorkflowEngine

        engine = WorkflowEngine()
        assert engine.add_directive("missing", {"action": "inject", "instruction": "x"}) is False


class TestPauseResumeGraceful:
    async def test_pause_finishes_current_step_then_holds(self, three_node_workflow):
        from fusion_cowork.engine.workflow import WorkflowEngine

        engine = WorkflowEngine()
        task = asyncio.create_task(engine.execute(three_node_workflow, execution_id="exec-p1"))
        await _pause_after_first(engine, "exec-p1")

        # graceful: 当前节点已完成落盘, 后续未跑
        assert engine.is_paused("exec-p1")
        engine.resume("exec-p1")
        execution = await asyncio.wait_for(task, timeout=5)

        assert execution.status.value == "success"
        assert [s.node_id for s in execution.steps if not s.user_correction] == ["n1", "n2", "n3"]
        assert all(s.status.value == "success" for s in execution.steps if not s.user_correction)
        # 产物一致: 每步输出都在
        assert all("echo" in s.output_data for s in execution.steps if not s.user_correction)


class TestInjectRouteAndStepDict:
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

    def test_inject_directive_queued(self, client):
        tc, app = client
        registry = app.state.run_registry
        self._seed(registry, "run_227", "paused")
        registry._engine.add_directive = lambda rid, d: True

        r = tc.post(
            "/api/runs/run_227/directive",
            json={"action": "inject", "instruction": "只看华东区"},
            headers={"Authorization": f"Bearer {STATIC_KEY}"},
        )
        assert r.status_code == 202
        assert registry._runs["run_227"]["directives"][0]["action"] == "inject"
        assert registry._runs["run_227"]["directives"][0]["payload"]["instruction"] == "只看华东区"

    def test_inject_empty_instruction_409(self, client):
        tc, app = client
        registry = app.state.run_registry
        self._seed(registry, "run_228", "paused")

        r = tc.post(
            "/api/runs/run_228/directive",
            json={"action": "inject", "instruction": ""},
            headers={"Authorization": f"Bearer {STATIC_KEY}"},
        )
        assert r.status_code == 409

    def test_step_dict_exposes_user_correction(self):
        from fusion_cowork.engine.node import NodeStatus
        from fusion_cowork.engine.workflow import WorkflowStep
        from fusion_cowork.server.web_api import RunRegistry

        marker = WorkflowStep(
            node_id="n2",
            node_name="inj_second",
            node_display_name="inj_second",
            status=NodeStatus.SKIPPED,
            summary="用户修正: x",
            user_correction=True,
        )
        plain = WorkflowStep(
            node_id="n1",
            node_name="inj_first",
            node_display_name="inj_first",
            status=NodeStatus.SUCCESS,
        )
        assert RunRegistry._step_dict(marker)["user_correction"] is True
        assert RunRegistry._step_dict(plain)["user_correction"] is False

    def test_directive_actions_include_inject(self):
        from fusion_cowork.server.web_api import RunRegistry

        assert "inject" in RunRegistry.DIRECTIVE_ACTIONS
