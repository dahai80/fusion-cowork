"""issue #303: run timeline attribution.

Webhook/web decisions now carry {approval_id, decided_by, decided_at} through
WorkflowEngine.resolve_hitl(attribution=...) onto the gate node's BLOCKED step
(output_data.hitl_attribution + human-readable summary), so the timeline can
answer "who decided". Terminal sweeps drop the mapping; runs without
attribution stay byte-identical.
"""

import asyncio

import pytest

from fusion_cowork.engine import PermissionLevel, PermissionManager
from fusion_cowork.engine.node import BaseNode, NodeConfig, NodeRegistry, NodeResult, NodeStatus, register_node
from fusion_cowork.engine.workflow import Edge, Workflow, WorkflowEngine
from fusion_cowork.nodes import import_all_nodes

import_all_nodes()


@pytest.fixture(autouse=True)
def _hitl_on(monkeypatch):
    monkeypatch.delenv("FUSION_COWORK_HITL_PAUSE", raising=False)
    monkeypatch.delenv("FUSION_GUARD_ENABLED", raising=False)


@register_node
class Probe303(BaseNode):
    name = "probe_303"

    async def execute(self, inputs):
        return NodeResult(status=NodeStatus.SUCCESS, data={"ok": True}, summary="ok")


def _gate_workflow():
    wf = Workflow(name="wf303")
    wf.add_node(NodeRegistry.create("approval_gate", node_id="g1", config=NodeConfig()))
    wf.add_node(NodeRegistry.create("probe_303", node_id="p1", config=NodeConfig()))
    wf.edges.append(Edge(source_id="g1", target_id="p1"))
    return wf


def _blocked_step(execution, node_id):
    return next(s for s in execution.steps if s.node_id == node_id and s.status == NodeStatus.BLOCKED)


class TestAttribution:
    @pytest.mark.asyncio
    async def test_webhook_style_attribution_lands_on_gate_step(self):
        pm = PermissionManager(level=PermissionLevel.CONFIRM)
        eng = WorkflowEngine(permission_manager=pm)
        wf = _gate_workflow()

        async def resolver():
            while True:
                await asyncio.sleep(0.02)
                for action_id in list(pm._pending_local_approvals.keys()):
                    exec_id = (pm.get_pending_details(action_id) or {}).get("execution_id", "")
                    if not exec_id:
                        continue
                    pm.release_pending_once(action_id)
                    eng.resolve_hitl(
                        exec_id,
                        True,
                        once=True,
                        attribution={
                            "approval_id": action_id,
                            "decided_by": "owner",
                            "decided_at": "2026-11-05T00:00:00Z",
                        },
                    )

        task = asyncio.create_task(resolver())
        try:
            execution = await asyncio.wait_for(eng.execute(wf, execution_id="run_303_attr"), timeout=10)
        finally:
            task.cancel()
        assert execution.status.value == "success"
        gate_step = _blocked_step(execution, "g1")
        attr = gate_step.output_data.get("hitl_attribution")
        assert attr == {
            "approval_id": attr["approval_id"],
            "decided_by": "owner",
            "decided_at": "2026-11-05T00:00:00Z",
        }
        assert "owner" in gate_step.summary

    @pytest.mark.asyncio
    async def test_deny_attribution_recorded(self):
        pm = PermissionManager(level=PermissionLevel.CONFIRM)
        eng = WorkflowEngine(permission_manager=pm)
        wf = _gate_workflow()

        async def denier():
            while True:
                await asyncio.sleep(0.02)
                for action_id in list(pm._pending_local_approvals.keys()):
                    exec_id = (pm.get_pending_details(action_id) or {}).get("execution_id", "")
                    if not exec_id:
                        continue
                    pm.release_pending_once(action_id)
                    eng.resolve_hitl(exec_id, False, attribution={"approval_id": action_id, "decided_by": "web"})

        task = asyncio.create_task(denier())
        try:
            execution = await asyncio.wait_for(eng.execute(wf, execution_id="run_303_deny"), timeout=10)
        finally:
            task.cancel()
        assert execution.status.value == "failed"
        gate_step = _blocked_step(execution, "g1")
        assert gate_step.output_data["hitl_attribution"]["decided_by"] == "web"
        assert "拒绝" in gate_step.summary

    @pytest.mark.asyncio
    async def test_no_attribution_keeps_step_clean(self):
        pm = PermissionManager(level=PermissionLevel.CONFIRM)
        eng = WorkflowEngine(permission_manager=pm)
        wf = _gate_workflow()

        async def resolver():
            while True:
                await asyncio.sleep(0.02)
                for action_id in list(pm._pending_local_approvals.keys()):
                    exec_id = (pm.get_pending_details(action_id) or {}).get("execution_id", "")
                    if not exec_id:
                        continue
                    pm.release_pending_once(action_id)
                    eng.resolve_hitl(exec_id, True, once=True)

        task = asyncio.create_task(resolver())
        try:
            execution = await asyncio.wait_for(eng.execute(wf, execution_id="run_303_plain"), timeout=10)
        finally:
            task.cancel()
        gate_step = _blocked_step(execution, "g1")
        assert "hitl_attribution" not in (gate_step.output_data or {})

    def test_resolve_hitl_without_pause_event_returns_false(self):
        pm = PermissionManager(level=PermissionLevel.CONFIRM)
        eng = WorkflowEngine(permission_manager=pm)
        assert eng.resolve_hitl("run_missing", True, attribution={"decided_by": "x"}) is False
