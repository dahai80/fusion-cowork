"""issue #508: approval checkpoint extensions on the HITL plane.

- Structured human reply: resolve_hitl(reply={...}) is stashed on approve and
  merged into the gated node input as ``human_reply``; approval_gate passes it
  through to its output data so downstream nodes read the human's answer.
- Per-node timeout policy: ``timeout_policy.timeout_s`` overrides the engine
  global HITL deadline; ``on_timeout: "deny"`` synthesizes a deny decision and
  routes through the common deny path (revision loop still registers), while
  the default stays fail-fast terminal.
- Awaiting state: engine.hitl_waiting_node exposes which node is waiting on a
  human decision; run detail exposes awaiting_human/awaiting_node.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from fusion_cowork.engine import PermissionLevel, PermissionManager
from fusion_cowork.engine.node import BaseNode, NodeCategory, NodeRegistry, NodeResult, NodeStatus, register_node
from fusion_cowork.engine.workflow import Workflow, WorkflowEngine

# imports for side-effect registration of the real approval_gate node
from fusion_cowork.nodes.ops import ops_nodes  # noqa: F401
from fusion_cowork.nodes.tools import tool_nodes  # noqa: F401


@register_node
class _Producer508(BaseNode):
    name = "producer_508"
    display_name = "Producer 508"
    category = NodeCategory.TOOL
    description = "test producer"
    icon = "📦"
    default_label = "Producer"
    runs: list = []

    async def execute(self, inputs: dict) -> NodeResult:
        type(self).runs.append(dict(inputs))
        return NodeResult(status=NodeStatus.SUCCESS, data={"draft": f"v{len(type(self).runs)}"}, summary="ok")


@pytest.fixture
def pm_508(monkeypatch):
    monkeypatch.delenv("FUSION_GUARD_ENABLED", raising=False)
    monkeypatch.delenv("FUSION_COWORK_HITL_PAUSE", raising=False)
    monkeypatch.delenv("FUSION_COWORK_HITL_TIMEOUT", raising=False)
    from fusion_cowork.security import guard as guard_mod

    monkeypatch.setattr(guard_mod, "guard_enabled", lambda: False)
    from fusion_cowork.engine import permission_manager

    monkeypatch.setattr(
        permission_manager,
        "HIGH_RISK_NODES",
        permission_manager.HIGH_RISK_NODES | {"approval_gate"},
    )
    return PermissionManager(level=PermissionLevel.CONFIRM)


def _wf(gate_params: dict, with_downstream: bool = False):
    _Producer508.runs = []
    wf = Workflow(name="checkpoint-508")
    n1 = NodeRegistry.create("producer_508")
    n1.id = "n1"
    gate = NodeRegistry.create("approval_gate")
    gate.id = "gate"
    gate.config.params = dict(gate_params)
    wf.add_node(n1)
    wf.add_node(gate)
    wf.connect("n1", "gate")
    if with_downstream:
        nd = NodeRegistry.create("producer_508")
        nd.id = "down"
        wf.add_node(nd)
        wf.connect("gate", "down")
    return wf


async def _wait_hitl(engine: WorkflowEngine, exec_id: str, timeout: float = 5.0) -> bool:
    for _ in range(int(timeout * 50)):
        if engine.hitl_pending(exec_id):
            return True
        await asyncio.sleep(0.02)
    return False


class TestStructuredReply:
    async def test_reply_flows_into_gate_input_and_output(self, pm_508):
        eng = WorkflowEngine(permission_manager=pm_508, hitl_pause=True)
        exec_id = "run-508-reply"
        task = asyncio.create_task(eng.execute(_wf({"prompt": "ok?"}, with_downstream=True), execution_id=exec_id))
        assert await _wait_hitl(eng, exec_id)
        assert eng.resolve_hitl(exec_id, True, once=True, reply={"choice": "a", "note": "use plan b"})
        execution = await asyncio.wait_for(task, timeout=5)
        assert execution.status.value == "success"
        gate_step = next(s for s in reversed(execution.steps) if s.node_id == "gate")
        assert gate_step.input_data.get("human_reply") == {"choice": "a", "note": "use plan b"}
        assert gate_step.output_data.get("human_reply") == {"choice": "a", "note": "use plan b"}
        down_step = next(s for s in execution.steps if s.node_id == "down")
        assert down_step.input_data.get("human_reply") == {"choice": "a", "note": "use plan b"}

    async def test_reply_ignored_on_deny(self, pm_508):
        eng = WorkflowEngine(permission_manager=pm_508, hitl_pause=True)
        exec_id = "run-508-reply-deny"
        task = asyncio.create_task(eng.execute(_wf({"prompt": "ok?"}), execution_id=exec_id))
        assert await _wait_hitl(eng, exec_id)
        assert eng.resolve_hitl(exec_id, False, reply={"choice": "a"})
        execution = await asyncio.wait_for(task, timeout=5)
        assert execution.status.value == "failed"
        assert eng._hitl_replies.get(exec_id) is None
        gate_step = next(s for s in reversed(execution.steps) if s.node_id == "gate")
        assert gate_step.status == NodeStatus.DENIED

    async def test_reply_without_approval_flag_not_stashed(self, pm_508):
        eng = WorkflowEngine(permission_manager=pm_508, hitl_pause=True)
        assert eng.resolve_hitl("run-508-missing", True, once=True, reply={"x": 1}) is False
        assert eng._hitl_replies == {}


class TestPerNodeTimeoutPolicy:
    async def test_timeout_s_overrides_default(self, pm_508):
        # engine default is 1800s; node policy 0.2s must auto-deny fast
        eng = WorkflowEngine(permission_manager=pm_508, hitl_pause=True)
        exec_id = "run-508-quick-timeout"
        started = time.time()
        execution = await asyncio.wait_for(
            eng.execute(_wf({"prompt": "ok?", "timeout_policy": {"timeout_s": 0.2}}), execution_id=exec_id),
            timeout=10,
        )
        assert execution.status.value == "failed"
        assert time.time() - started < 5
        gate_step = next(s for s in reversed(execution.steps) if s.node_id == "gate")
        assert gate_step.status == NodeStatus.DENIED

    async def test_on_timeout_deny_enters_revision_loop(self, pm_508):
        # on_timeout="deny" synthesizes the deny decision and registers the
        # revision, so the producer re-runs and the gate asks again fresh
        eng = WorkflowEngine(permission_manager=pm_508, hitl_pause=True)
        exec_id = "run-508-timeout-deny"
        gate_params = {
            "prompt": "ok?",
            "timeout_policy": {"timeout_s": 0.2, "on_timeout": "deny"},
            "on_reject": "revise",
            "revision_node": "n1",
        }
        task = asyncio.create_task(eng.execute(_wf(gate_params), execution_id=exec_id))
        # first gate ask times out -> synthesized deny -> revision -> fresh re-ask;
        # only one pending exists at a time (each timeout discards the previous one)
        for _ in range(200):
            if len(_Producer508.runs) >= 2 and pm_508.list_pending():
                break
            await asyncio.sleep(0.05)
        assert len(_Producer508.runs) == 2, "timeout deny must route the producer back for revision"
        pendings = [p for p in pm_508.list_pending() if p.get("execution_id") == exec_id]
        assert len(pendings) == 1, "re-asked gate must wait for a fresh approval"
        action_id = pendings[0]["action_id"]
        assert pm_508.release_pending_once(action_id) is True
        assert eng.resolve_hitl(exec_id, True, once=True)
        execution = await asyncio.wait_for(task, timeout=5)
        assert execution.status.value == "completed_with_denials"
        assert len(_Producer508.runs) == 2, "producer must re-run with revision comments"

    async def test_garbage_policy_uses_default(self, pm_508, monkeypatch):
        # malformed timeout_policy must not crash and must not shrink the deadline
        monkeypatch.setenv("FUSION_COWORK_HITL_TIMEOUT", "30s")
        eng = WorkflowEngine(permission_manager=pm_508, hitl_pause=True)
        exec_id = "run-508-garbage"
        task = asyncio.create_task(
            eng.execute(_wf({"prompt": "ok?", "timeout_policy": {"timeout_s": "abc"}}), execution_id=exec_id)
        )
        assert await _wait_hitl(eng, exec_id)
        assert eng.hitl_deadline(exec_id) is not None
        eng.resolve_hitl(exec_id, True, once=True)
        execution = await asyncio.wait_for(task, timeout=5)
        assert execution.status.value == "success"


class TestAwaitingState:
    async def test_waiting_node_visible_and_cleared(self, pm_508):
        eng = WorkflowEngine(permission_manager=pm_508, hitl_pause=True)
        exec_id = "run-508-awaiting"
        task = asyncio.create_task(eng.execute(_wf({"prompt": "ok?"}), execution_id=exec_id))
        assert await _wait_hitl(eng, exec_id)
        assert eng.hitl_waiting_node(exec_id) == "gate"
        assert eng.resolve_hitl(exec_id, True, once=True)
        execution = await asyncio.wait_for(task, timeout=5)
        assert execution.status.value == "success"
        assert eng.hitl_waiting_node(exec_id) is None

    def test_run_detail_awaiting_fields_and_reply(self, pm_508, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient

        from fusion_cowork.server import web_api

        monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", "test-key-508")
        eng = WorkflowEngine(permission_manager=pm_508, hitl_pause=True)
        app = web_api.create_web_app(engine=eng, dist_dir=tmp_path / "no-dist", store_root=tmp_path / "store")
        auth = {"Authorization": "Bearer test-key-508"}
        wf_def = {
            "name": "checkpoint-wf-508",
            "workflow": {
                "nodes": [
                    {"id": "n1", "name": "producer_508", "config": {}},
                    {"id": "gate", "name": "approval_gate", "config": {"params": {"prompt": "ok?"}}},
                ],
                "edges": [{"source_id": "n1", "target_id": "gate"}],
            },
        }
        with TestClient(app) as client:
            wf_id = client.post("/api/workflows", json=wf_def, headers=auth).json()["id"]
            run_id = client.post("/api/runs", json={"workflow_id": wf_id}, headers=auth).json()["run_id"]
            for _ in range(200):
                if eng.hitl_pending(run_id):
                    break
                time.sleep(0.05)
            assert eng.hitl_pending(run_id)
            detail = client.get(f"/api/runs/{run_id}", headers=auth).json()
            assert detail["hitl_pending"] is True
            assert detail["awaiting_human"] is True
            assert detail["awaiting_node"] == "gate"
            pending = client.get("/api/permissions/pending", headers=auth).json()["approvals"]
            action_id = pending[0]["action_id"]
            confirm = client.post(
                f"/api/permissions/pending/{action_id}/confirm",
                json={"approved": True, "once": True, "reply": {"choice": "b"}},
                headers=auth,
            )
            assert confirm.status_code == 200, confirm.text
            for _ in range(200):
                detail = client.get(f"/api/runs/{run_id}", headers=auth).json()
                if detail.get("status") != "running":
                    break
                time.sleep(0.05)
            assert detail["status"] in ("success", "completed")
            gate_step = next(s for s in reversed(detail["steps"]) if s["node_id"] == "gate")
            assert gate_step["input_data"].get("human_reply") == {"choice": "b"}
            assert gate_step["output_data"].get("human_reply") == {"choice": "b"}


class TestGateNodeUnit:
    async def test_gate_output_passes_human_reply_through(self):

        node = NodeRegistry.create("approval_gate")
        node.config.params = {"prompt": "ok?"}
        result = await node.execute({"payload": {"x": 1}, "human_reply": {"choice": "a"}})
        assert result.data["human_reply"] == {"choice": "a"}
        assert result.data["approved"] is True
        assert "回复" in result.summary  # i18n: user-facing

    def test_gate_schema_declares_timeout_policy(self):

        node = NodeRegistry.create("approval_gate")
        schema = node.get_params_schema()
        policy = schema["properties"]["timeout_policy"]
        assert policy["properties"]["on_timeout"]["enum"] == ["deny", "fail"]
        assert "timeout_s" in policy["properties"]
