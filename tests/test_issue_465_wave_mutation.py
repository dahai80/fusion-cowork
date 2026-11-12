"""issue #465: 波边界动态变更 — remove_node / retarget_edge 指令 + 结构化拒绝。

波边界才生效 (防线 4): 波内目标 mid_wave 结构化拒绝; 已执行 already_executed;
节点/边不存在 unknown_target; 字段缺失 bad_directive。API 409/400 透传。
"""

from __future__ import annotations

import asyncio
import itertools

from fastapi.testclient import TestClient

from fusion_cowork.engine import PermissionManager
from fusion_cowork.engine.node import BaseNode, NodeCategory, NodeRegistry, NodeResult, NodeStatus, register_node
from fusion_cowork.engine.workflow import Workflow, WorkflowEngine, WorkflowStep
from fusion_cowork.nodes import import_all_nodes

FLAG_465 = {"queued": False}


@register_node
class _Gate465(BaseNode):
    name = "gate465"
    display_name = "Gate 465"
    category = NodeCategory.LOGIC
    description = "test probe: waits until directive queued (#465)"
    icon = "🧪"
    default_label = "Gate"

    async def execute(self, inputs: dict) -> NodeResult:
        for _ in range(250):
            if FLAG_465["queued"]:
                break
            await asyncio.sleep(0.02)
        return NodeResult(status=NodeStatus.SUCCESS, data={"gate": True}, summary="gate")


@register_node
class _Echo465(BaseNode):
    name = "echo465"
    display_name = "Echo 465"
    category = NodeCategory.LOGIC
    description = "test probe: passes seed through (#465)"
    icon = "🧪"
    default_label = "Echo"

    calls = 0

    async def execute(self, inputs: dict) -> NodeResult:
        type(self).calls += 1
        return NodeResult(status=NodeStatus.SUCCESS, data={"seed": str(inputs.get("seed", ""))}, summary="echo")


def _setup():
    import_all_nodes()


def _engine():
    return WorkflowEngine(permission_manager=PermissionManager())


def _chain(*names):
    wf = Workflow(name="wf-465")
    ids = [wf.add_node(NodeRegistry.create(n)) for n in names]
    for a, b in itertools.pairwise(ids):
        wf.connect(a, b)
    return wf, ids


class TestValidateMutation:
    def test_mid_wave_rejected(self):
        _setup()
        eng = _engine()
        wf, ids = _chain("echo465", "echo465", "echo465")
        eng._run_workflows["r"] = wf
        eng._wave_running["r"] = {ids[1]}
        eng._pause_events["r"] = asyncio.Event()
        ok = eng.add_directive("r", {"action": "remove_node", "node_id": ids[1]})
        assert ok is False
        err = eng.pop_directive_error("r")
        assert err is not None and err["code"] == "mid_wave"
        assert ids[1] in wf.nodes, "拒绝的指令不得改变工作流"

    def test_already_executed_rejected(self):
        _setup()
        eng = _engine()
        wf, ids = _chain("echo465", "echo465")
        eng._run_workflows["r"] = wf
        execution = _fake_execution()
        eng._executions["r"] = execution
        execution.steps.append(
            WorkflowStep(node_id=ids[0], node_name="echo465", node_display_name="Echo", status=NodeStatus.SUCCESS)
        )
        ok = eng.add_directive("r", {"action": "remove_node", "node_id": ids[0]})
        assert ok is False
        assert eng.pop_directive_error("r")["code"] == "already_executed"

    def test_bad_and_unknown_rejected(self):
        _setup()
        eng = _engine()
        wf, ids = _chain("echo465", "echo465")
        eng._run_workflows["r"] = wf
        eng._pause_events["r"] = asyncio.Event()
        assert eng.add_directive("r", {"action": "remove_node"}) is False
        assert eng.pop_directive_error("r")["code"] == "bad_directive"
        d = {"action": "retarget_edge", "source_id": ids[0], "target_id": "ghost", "new_target_id": ids[1]}
        assert eng.add_directive("r", d) is False
        assert eng.pop_directive_error("r")["code"] == "unknown_target"
        d2 = {"action": "retarget_edge", "source_id": ids[0], "target_id": ids[1], "new_target_id": "ghost"}
        assert eng.add_directive("r", d2) is False
        assert eng.pop_directive_error("r")["code"] == "unknown_target"

    def test_valid_mutation_queued(self):
        _setup()
        eng = _engine()
        wf, ids = _chain("echo465", "echo465", "echo465")
        eng._run_workflows["r"] = wf
        eng._pause_events["r"] = asyncio.Event()
        assert eng.add_directive("r", {"action": "remove_node", "node_id": ids[2]}) is True
        assert (
            eng.add_directive(
                "r", {"action": "retarget_edge", "source_id": ids[0], "target_id": ids[1], "new_target_id": ids[2]}
            )
            is True
        )
        assert eng.pop_directive_error("r") is None


class TestApplyAtBoundary:
    def test_remove_node_drops_from_order_and_edges(self):
        _setup()
        eng = _engine()
        wf, ids = _chain("echo465", "echo465", "echo465")
        eng._directives["r"] = [{"action": "remove_node", "node_id": ids[1]}]
        order = list(ids)
        cursor = eng._apply_directives("r", wf, order, 0, {}, {}, _fake_execution())
        assert ids[1] not in wf.nodes
        assert all(e.source_id != ids[1] and e.target_id != ids[1] for e in wf.edges)
        assert ids[1] not in order
        assert order == [ids[0], ids[2]]
        assert cursor == 0

    def test_retarget_edge_mutates_edge(self):
        _setup()
        eng = _engine()
        wf, ids = _chain("echo465", "echo465", "echo465")
        eng._directives["r"] = [
            {"action": "retarget_edge", "source_id": ids[0], "target_id": ids[1], "new_target_id": ids[2]}
        ]
        eng._apply_directives("r", wf, list(ids), 1, {}, {}, _fake_execution())
        assert any(e.source_id == ids[0] and e.target_id == ids[2] for e in wf.edges)
        assert not any(e.source_id == ids[0] and e.target_id == ids[1] for e in wf.edges)


class TestLiveRun:
    async def test_remove_node_lands_at_next_boundary(self):
        _setup()
        FLAG_465["queued"] = False
        eng = _engine()
        wf, ids = _chain("gate465", "echo465", "echo465")
        _Echo465.calls = 0
        exec_id = "run-465-remove"

        async def queue_when_running():
            for _ in range(250):
                if exec_id in eng._wave_running:
                    break
                await asyncio.sleep(0.02)
            ok = eng.add_directive(exec_id, {"action": "remove_node", "node_id": ids[2]})
            FLAG_465["queued"] = ok
            assert ok is True

        task = asyncio.create_task(eng.execute(wf, execution_id=exec_id))
        await queue_when_running()
        execution = await asyncio.wait_for(task, timeout=10)
        assert execution.status.value == "success", execution.error
        ran = {s.node_id for s in execution.steps if s.status == NodeStatus.SUCCESS}
        assert ids[2] not in ran, "被移除节点不得执行"
        assert ids[1] in ran

    async def test_retarget_edge_run_succeeds(self):
        _setup()
        FLAG_465["queued"] = False
        eng = _engine()
        wf, ids = _chain("gate465", "echo465", "echo465")
        exec_id = "run-465-retarget"

        async def queue_when_running():
            for _ in range(250):
                if exec_id in eng._wave_running:
                    break
                await asyncio.sleep(0.02)
            ok = eng.add_directive(
                exec_id, {"action": "retarget_edge", "source_id": ids[0], "target_id": ids[1], "new_target_id": ids[2]}
            )
            FLAG_465["queued"] = ok
            assert ok is True

        task = asyncio.create_task(eng.execute(wf, execution_id=exec_id))
        await queue_when_running()
        execution = await asyncio.wait_for(task, timeout=10)
        assert execution.status.value == "success", execution.error


class TestApiRoute:
    def test_directive_structured_error_http(self, monkeypatch, tmp_path):
        from fusion_cowork.server.web_api import create_web_app

        FLAG_465["queued"] = False
        monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", "test-key-465")
        headers = {"Authorization": "Bearer test-key-465"}
        eng = _engine()
        app = create_web_app(engine=eng, store_root=tmp_path / "store")
        wf_def = {
            "name": "wf-465-api",
            "nodes": [
                {"id": "g", "name": "gate465", "config": {}},
                {"id": "e", "name": "echo465", "config": {}},
            ],
            "edges": [{"source_id": "g", "target_id": "e"}],
        }
        with TestClient(app) as client:
            r = client.post("/api/runs", json={"workflow": wf_def}, headers=headers)
            assert r.status_code in (200, 201, 202), r.text
            run_id = r.json()["run_id"]
            for _ in range(250):
                if "g" in eng._wave_running.get(run_id, set()):
                    break
                asyncio.run(asyncio.sleep(0.02))
            # 波内目标 (gate465 执行中) → 结构化拒绝
            r2 = client.post(
                f"/api/runs/{run_id}/directive", json={"action": "remove_node", "node_id": "g"}, headers=headers
            )
            assert r2.status_code == 409, r2.text
            assert r2.json()["detail"]["code"] == "mid_wave"
            # 字段缺失 → 400
            r3 = client.post(f"/api/runs/{run_id}/directive", json={"action": "remove_node"}, headers=headers)
            assert r3.status_code == 400
            assert r3.json()["detail"]["code"] == "bad_directive"
            client.post(f"/api/runs/{run_id}/cancel", headers=headers)


def _fake_execution():
    from fusion_cowork.engine.workflow import WorkflowExecution, WorkflowStatus

    return WorkflowExecution(id="r", workflow_id="", workflow_name="", status=WorkflowStatus.RUNNING, started_at=0.0)
