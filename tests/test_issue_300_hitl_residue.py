"""issue #300: HITL residue cleanup at run terminal state.

Two residue symptoms from live acceptance: (1) GET /api/runs/{id} returned
hitl_pending=true on a terminal success run, (2) a local approval pending
(source=local, tool=approval_gate) survived forever after the webhook decision.
Normal wake/timeout paths discard state correctly (repro-verified clean), so the
fix is a terminal-state sweep in WorkflowEngine.execute: _hitl_waiting,
_hitl_deadlines and any leftover permission pendings for the execution are
discarded when the run ends, whatever path left them behind.
"""

import asyncio
import logging

import pytest

from fusion_cowork.engine import PermissionLevel, PermissionManager
from fusion_cowork.engine.node import (
    BaseNode,
    NodeConfig,
    NodeRegistry,
    NodeResult,
    NodeStatus,
    register_node,
)
from fusion_cowork.engine.workflow import Edge, Workflow, WorkflowEngine
from fusion_cowork.nodes import import_all_nodes

import_all_nodes()


@pytest.fixture(autouse=True)
def _hitl_off(monkeypatch):
    monkeypatch.setenv("FUSION_COWORK_HITL_PAUSE", "0")
    monkeypatch.delenv("FUSION_GUARD_ENABLED", raising=False)


def _make_engine():
    pm = PermissionManager(level=PermissionLevel.CONFIRM)
    return pm, WorkflowEngine(permission_manager=pm)


def _gate_workflow():
    wf = Workflow(name="wf300")
    wf.add_node(NodeRegistry.create("approval_gate", node_id="g1", config=NodeConfig()))
    wf.add_node(NodeRegistry.create("probe_300", node_id="p1", config=NodeConfig()))
    wf.edges.append(Edge(source_id="g1", target_id="p1"))
    return wf


@register_node
class Probe300(BaseNode):
    name = "probe_300"

    async def execute(self, inputs):
        return NodeResult(status=NodeStatus.SUCCESS, data={"ok": True}, summary="ok")


class TestResolvedFlowLeavesNoResidue:
    @pytest.mark.asyncio
    async def test_terminal_success_clears_hitl_state_and_pendings(self, monkeypatch):
        monkeypatch.delenv("FUSION_COWORK_HITL_PAUSE", raising=False)
        pm, eng = _make_engine()
        wf = _gate_workflow()

        async def webhook_style_resolver():
            # emulate the signed webhook route: release the local pending once
            # and resolve the paused run with a once-grant
            while True:
                await asyncio.sleep(0.02)
                for action_id in list(pm._pending_local_approvals.keys()):
                    details = pm.get_pending_details(action_id) or {}
                    exec_id = details.get("execution_id", "")
                    if not exec_id:
                        continue
                    pm.release_pending_once(action_id)
                    eng.resolve_hitl(exec_id, True, once=True)

        task = asyncio.create_task(webhook_style_resolver())
        try:
            execution = await asyncio.wait_for(eng.execute(wf, execution_id="run_300_clean"), timeout=10)
        finally:
            task.cancel()
        assert execution.status.value == "success"
        assert eng.hitl_pending("run_300_clean") is False
        assert eng._hitl_waiting == set()
        assert eng._hitl_deadlines == {}
        assert pm.list_pending() == []
        assert pm.find_pending_by_execution("run_300_clean") is None
        assert eng._pause_events == {}


class TestTerminalSweep:
    @pytest.mark.asyncio
    async def test_sweep_clears_seeded_residue_on_failed_run(self, caplog):
        # simulate any leak path: residue present while the run goes terminal
        pm, eng = _make_engine()
        wf = _gate_workflow()
        pm._pending_local_approvals["act_stale"] = "approval_gate"
        pm._pending_local_details["act_stale"] = {
            "tool_name": "approval_gate",
            "execution_id": "run_300_stale",
            "node_id": "g1",
        }
        eng._hitl_waiting.add("run_300_stale")
        eng._hitl_deadlines["run_300_stale"] = 9e9
        with caplog.at_level(logging.WARNING):
            execution = await eng.execute(wf, execution_id="run_300_stale")
        assert execution.status.value == "failed"
        assert eng.hitl_pending("run_300_stale") is False
        assert pm.find_pending_by_execution("run_300_stale") is None
        assert pm.list_pending() == []
        assert "终态清理" in caplog.text

    @pytest.mark.asyncio
    async def test_sweep_silent_when_no_residue(self, caplog):
        pm, eng = _make_engine()
        wf = Workflow(name="wf300_ok")
        wf.add_node(NodeRegistry.create("probe_300", node_id="p1", config=NodeConfig()))
        with caplog.at_level(logging.WARNING):
            execution = await eng.execute(wf, execution_id="run_300_ok")
        assert execution.status.value == "success"
        assert "终态清理" not in caplog.text

    @pytest.mark.asyncio
    async def test_sweep_scoped_to_own_execution(self):
        # residue of another run must survive this run's terminal sweep
        pm, eng = _make_engine()
        wf = Workflow(name="wf300_ok")
        wf.add_node(NodeRegistry.create("probe_300", node_id="p1", config=NodeConfig()))
        pm._pending_local_approvals["act_other"] = "approval_gate"
        pm._pending_local_details["act_other"] = {
            "tool_name": "approval_gate",
            "execution_id": "run_other",
            "node_id": "g1",
        }
        await eng.execute(wf, execution_id="run_300_own")
        assert pm.find_pending_by_execution("run_other") is not None
