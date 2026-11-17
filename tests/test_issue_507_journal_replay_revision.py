"""issue #507: durable HITL journal replay must not kill the revision loop.

#463 replay keyed on (run_id, node_id) re-applied the first deny when the
#328 revision loop re-asked the same gate node — the second ask auto-denied
without waiting, the run failed and the fresh pending row dangled. Decisions
are now consumed on use (replay / normal resolve / timeout), so a re-asked
gate always gets a fresh evaluation. Crash-restart replay still works
(consume happens exactly when the decision is applied).
"""

from __future__ import annotations

import asyncio

import pytest

from fusion_cowork.engine import PermissionLevel, PermissionManager
from fusion_cowork.engine.approval_journal import ApprovalJournal
from fusion_cowork.engine.node import (
    BaseNode,
    NodeCategory,
    NodeConfig,
    NodeRegistry,
    NodeResult,
    NodeStatus,
    register_node,
)
from fusion_cowork.engine.workflow import Workflow, WorkflowEngine

GATE_NODE = "gate_507"


@register_node
class _ProducerNode(BaseNode):
    name = "producer_507"
    display_name = "Producer 507"
    category = NodeCategory.TOOL
    description = "test producer: records each input it receives"
    icon = "📦"
    default_label = "Producer"

    runs: list = []

    async def execute(self, inputs: dict) -> NodeResult:
        type(self).runs.append(dict(inputs))
        return NodeResult(status=NodeStatus.SUCCESS, data={"draft": f"v{len(type(self).runs)}"}, summary="ok")


@register_node
class _GateNode(BaseNode):
    name = GATE_NODE
    display_name = "Gate 507"
    category = NodeCategory.TOOL
    description = "test gate (HITL carried by the permission layer)"
    icon = "🚧"
    default_label = "Gate"

    async def execute(self, inputs: dict) -> NodeResult:
        return NodeResult(status=NodeStatus.SUCCESS, data={"approved": True}, summary="ok")


@pytest.fixture
def pm_507(monkeypatch):
    monkeypatch.delenv("FUSION_GUARD_ENABLED", raising=False)
    monkeypatch.delenv("FUSION_COWORK_HITL_PAUSE", raising=False)
    from fusion_cowork.security import guard as guard_mod

    monkeypatch.setattr(guard_mod, "guard_enabled", lambda: False)
    from fusion_cowork.engine import permission_manager

    monkeypatch.setattr(permission_manager, "HIGH_RISK_NODES", permission_manager.HIGH_RISK_NODES | {GATE_NODE})
    return PermissionManager(level=PermissionLevel.CONFIRM)


def _wf(gate_params: dict) -> Workflow:
    wf = Workflow(name="revise-507")
    n1 = NodeRegistry.create("producer_507")
    n1.id = "n1"
    n2 = NodeRegistry.create(GATE_NODE)
    n2.id = "n2"
    n2.config = NodeConfig(params=dict(gate_params))
    wf.add_node(n1)
    wf.add_node(n2)
    wf.connect("n1", "n2")
    return wf


async def _wait_hitl(engine: WorkflowEngine, exec_id: str, timeout: float = 5.0) -> bool:
    for _ in range(int(timeout * 50)):
        if engine.hitl_pending(exec_id):
            return True
        await asyncio.sleep(0.02)
    return False


async def _wait_pending_count(pm: PermissionManager, exec_id: str, count: int, timeout: float = 5.0) -> bool:
    for _ in range(int(timeout * 50)):
        entries = [p for p in pm.list_pending() if p.get("execution_id") == exec_id]
        if len(entries) >= count:
            return True
        await asyncio.sleep(0.02)
    return False


class TestJournalConsumedOnUse:
    async def test_revision_loop_survives_journal(self, pm_507, tmp_path):
        # nightly-20261117 cowork-c12 failure: deny -> revise -> gate re-ask was
        # auto-denied by the #463 replay of the first deny
        _ProducerNode.runs = []
        j = ApprovalJournal(tmp_path / "j.db")
        eng = WorkflowEngine(permission_manager=pm_507, hitl_pause=True, approval_journal=j)
        exec_id = "run-507-loop"
        task = asyncio.create_task(
            eng.execute(_wf({"on_reject": "revise", "revision_node": "n1"}), execution_id=exec_id)
        )
        assert await _wait_hitl(eng, exec_id)
        eng.resolve_hitl(exec_id, False, comments="revise: use 7 not 6")
        assert await _wait_pending_count(pm_507, exec_id, 2), "gate re-ask after revise must wait for fresh approval"
        eng.resolve_hitl(exec_id, True, once=True)
        execution = await asyncio.wait_for(task, timeout=5)
        assert execution.status.value == "completed_with_denials"
        assert len(_ProducerNode.runs) == 2
        assert _ProducerNode.runs[1].get("revision_comments") == "revise: use 7 not 6"
        assert j.decision_for(exec_id, "n2") is None, "consumed decisions must not replay"

    async def test_normal_decision_consumed(self, pm_507, tmp_path):
        _ProducerNode.runs = []
        j = ApprovalJournal(tmp_path / "j.db")
        eng = WorkflowEngine(permission_manager=pm_507, hitl_pause=True, approval_journal=j)
        exec_id = "run-507-approve"
        task = asyncio.create_task(eng.execute(_wf({}), execution_id=exec_id))
        assert await _wait_hitl(eng, exec_id)
        assert j.decision_for(exec_id, "n2") is None, "pending row must not look decided"
        eng.resolve_hitl(exec_id, True, once=True)
        execution = await asyncio.wait_for(task, timeout=5)
        assert execution.status.value == "success"
        assert j.decision_for(exec_id, "n2") is None, "applied decision must be consumed"

    async def test_timeout_decision_consumed(self, pm_507, tmp_path, monkeypatch):
        # timeout auto-deny takes the terminal deny-fast path (no revision re-ask),
        # but its journal row must still be consumed so no later replay revives it
        monkeypatch.setenv("FUSION_COWORK_HITL_TIMEOUT", "0.2")
        _ProducerNode.runs = []
        j = ApprovalJournal(tmp_path / "j.db")
        eng = WorkflowEngine(permission_manager=pm_507, hitl_pause=True, approval_journal=j)
        exec_id = "run-507-timeout"
        execution = await asyncio.wait_for(
            eng.execute(_wf({"on_reject": "revise", "revision_node": "n1"}), execution_id=exec_id), timeout=10
        )
        assert execution.status.value == "failed"
        assert len(_ProducerNode.runs) == 1
        assert j.decision_for(exec_id, "n2") is None, "timeout auto-deny must be consumed"
        assert j.pending() == []


class TestReplayStillWorksAfterRestart:
    async def test_replay_applies_once_then_spent(self, pm_507, tmp_path):
        # crash-restart contract kept: undecided-but-recorded decisions replay
        # exactly once (consumed on use), a later re-ask goes fresh
        j = ApprovalJournal(tmp_path / "j.db")
        node = NodeRegistry.create(GATE_NODE)
        j.record_pending("act-507-r", "run-507-replay", node_id=node.id, tool_name=GATE_NODE)
        j.resolve("act-507-r", True, decided_by="alice")
        assert j.decision_for("run-507-replay", node.id) is not None
        eng = WorkflowEngine(permission_manager=pm_507, hitl_pause=True, approval_journal=j)
        allowed = await eng._hitl_await("run-507-replay", _wf({}), node, {})
        assert allowed is True
        assert j.decision_for("run-507-replay", node.id) is None, "replayed decision must be consumed on use"
        # a second evaluation of the same node no longer replays — it must wait
        import types

        eng._pause_events["run-507-replay"] = asyncio.Event()

        async def _fresh_check(name, action, data):
            return False

        eng._permission_manager.check = _fresh_check
        finder = eng._permission_manager.find_pending_by_execution
        eng._permission_manager.find_pending_by_execution = (
            lambda exec_id: {"action_id": "act-507-fresh", "execution_id": exec_id}
        )
        try:
            task = asyncio.create_task(eng._hitl_await("run-507-replay", _wf({}), node, {}))
            await asyncio.sleep(0.1)
            assert not task.done(), "second evaluation must wait for fresh approval, not replay"
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            eng._permission_manager.find_pending_by_execution = finder
            assert types  # keep import local-only
