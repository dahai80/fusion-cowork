"""issue #558: P-series kernel knob adoption (fusion-harness #124/#125).

Item 1 (#125): engine wires cowork's KernelCheckpointBinder.store as
HumanGate(checkpoint_ref=...) — pending HITL questions mirror as
`awaiting_human` checkpoint entries keyed by run id; unwired binder =
plain gate (zero behavior change).

Item 5 (#124): SpaceKernelPlane evaluates member-count fan-out guidance
(fanout_warnings, FANOUT_* bands) at runtime spawn — non-fatal warnings
logged + surfaced on the plane, never blocking.
"""

from __future__ import annotations

import asyncio

import pytest
from fusion_harness import AWAITING_HUMAN_MARKER, HumanGate

from fusion_cowork.engine import PermissionLevel, PermissionManager
from fusion_cowork.engine.kernel_checkpoint import KernelCheckpointBinder
from fusion_cowork.engine.node import BaseNode, NodeCategory, NodeRegistry, NodeResult, NodeStatus, register_node
from fusion_cowork.engine.workflow import Workflow, WorkflowEngine
from fusion_cowork.space.kernel_plane import SpaceKernelPlane

HITL_NODE = "hitl_probe_558"


@register_node
class _ProbeNode(BaseNode):
    name = HITL_NODE
    display_name = "HITL Probe 558"
    category = NodeCategory.TOOL
    description = "test-only high-risk probe (#558)"
    icon = "🧪"
    default_label = "Probe"

    calls = 0

    async def execute(self, inputs: dict) -> NodeResult:
        type(self).calls += 1
        return NodeResult(status=NodeStatus.SUCCESS, data={"ok": True}, summary="ok")


@pytest.fixture
def pm_no_guard(monkeypatch):
    monkeypatch.delenv("FUSION_GUARD_ENABLED", raising=False)
    monkeypatch.delenv("FUSION_COWORK_HITL_PAUSE", raising=False)
    monkeypatch.delenv("FUSION_COWORK_HITL_TIMEOUT", raising=False)
    from fusion_cowork.engine import permission_manager

    monkeypatch.setattr(permission_manager, "HIGH_RISK_NODES", permission_manager.HIGH_RISK_NODES | {HITL_NODE})
    return PermissionManager(level=PermissionLevel.CONFIRM)


def _wf():
    wf = Workflow(name="hitl-wf-558")
    wf.add_node(NodeRegistry.create(HITL_NODE))
    return wf


async def _wait_hitl(engine: WorkflowEngine, exec_id: str, timeout: float = 5.0) -> bool:
    for _ in range(int(timeout * 50)):
        if engine.hitl_pending(exec_id):
            return True
        await asyncio.sleep(0.02)
    return False


class TestItem1CheckpointRef:
    def test_binder_store_property(self, tmp_path):
        binder = KernelCheckpointBinder(db_path=str(tmp_path / "ck.db"))
        assert binder.store is not None

    async def test_pending_question_mirrors_awaiting_human(self, tmp_path, pm_no_guard, monkeypatch):
        monkeypatch.setenv("FUSION_COWORK_HITL_GATE_DIR", str(tmp_path / "gate"))
        binder = KernelCheckpointBinder(db_path=str(tmp_path / "ck.db"))
        eng = WorkflowEngine(permission_manager=pm_no_guard, kernel_checkpoints=binder)
        eng._hitl_gate = None

        exec_id = "run-558-mirror"
        task = asyncio.create_task(eng.execute(_wf(), execution_id=exec_id))
        ok = await _wait_hitl(eng, exec_id)
        assert ok
        state = binder.store.latest(exec_id)
        assert state is not None
        assert state.permission_mode == AWAITING_HUMAN_MARKER
        awaiting = state.metadata["awaiting_human"]
        assert awaiting["session_id"] == exec_id
        assert awaiting["payload"]["tool_name"] == HITL_NODE
        assert awaiting["policy"] == "block"

        eng.resolve_hitl(exec_id, True, once=True)
        await asyncio.wait_for(task, timeout=10)

    async def test_resolve_updates_checkpoint_marker(self, tmp_path, pm_no_guard, monkeypatch):
        monkeypatch.setenv("FUSION_COWORK_HITL_GATE_DIR", str(tmp_path / "gate"))
        binder = KernelCheckpointBinder(db_path=str(tmp_path / "ck.db"))
        eng = WorkflowEngine(permission_manager=pm_no_guard, kernel_checkpoints=binder)
        eng._hitl_gate = None

        exec_id = "run-558-resolve"
        task = asyncio.create_task(eng.execute(_wf(), execution_id=exec_id))
        await _wait_hitl(eng, exec_id)
        eng.resolve_hitl(exec_id, True, once=True)
        await asyncio.wait_for(task, timeout=10)

        state = binder.store.latest(exec_id)
        awaiting = state.metadata.get("awaiting_human")
        if awaiting is not None:
            assert awaiting["checkpoint_kind"] != "ask"

    def test_unwired_binder_plain_gate(self, tmp_path, pm_no_guard, monkeypatch):
        monkeypatch.setenv("FUSION_COWORK_HITL_GATE_DIR", str(tmp_path / "gate"))
        eng = WorkflowEngine(permission_manager=pm_no_guard, kernel_checkpoints=None)
        gate = eng._build_kernel_gate()
        assert isinstance(gate, HumanGate)
        assert gate.checkpoint_ref is None

    def test_explicit_gate_not_rebuilt(self, pm_no_guard):
        injected = HumanGate("/tmp/hitl-gate-558-nonexistent")
        eng = WorkflowEngine(permission_manager=pm_no_guard, hitl_gate=injected)
        assert eng._build_kernel_gate() is injected


class TestItem5FanoutWarnings:
    def _plane(self, member_count: int):
        class _FakeStore:
            _data_dir = ""

            async def list_members(self, space_id):
                return []

        plane = SpaceKernelPlane(_FakeStore(), state_root="/tmp/skp-558")
        ids = [f"u{i}" for i in range(member_count)]
        plane._roster_cache["s1"] = ids
        return plane

    async def test_small_roster_no_warning(self, monkeypatch, tmp_path):
        from fusion_cowork.space import kernel_plane as kp

        async def _roster(self, space_id):
            return [f"u{i}" for i in range(3)]

        monkeypatch.setattr(SpaceKernelPlane, "_roster", _roster)
        monkeypatch.setattr(kp, "_kernel_name", lambda mid: f"k-{mid}")
        plane = SpaceKernelPlane(object(), state_root=str(tmp_path / "st"))
        await plane._ensure_runtime("s1")
        assert plane._fanout_warnings.get("s1") is None

    async def test_oversized_roster_warns(self, monkeypatch, tmp_path):
        from fusion_cowork.space import kernel_plane as kp

        async def _roster(self, space_id):
            return [f"u{i}" for i in range(16)]

        monkeypatch.setattr(SpaceKernelPlane, "_roster", _roster)
        monkeypatch.setattr(kp, "_kernel_name", lambda mid: f"k-{mid}")
        plane = SpaceKernelPlane(object(), state_root=str(tmp_path / "st"))
        await plane._ensure_runtime("s1")
        warnings = plane._fanout_warnings.get("s1")
        assert warnings
        assert any("16" in w for w in warnings)

    async def test_warning_cleared_when_roster_shrinks(self, monkeypatch, tmp_path):
        from fusion_cowork.space import kernel_plane as kp

        counts = {"n": 16}

        async def _roster(self, space_id):
            return [f"u{i}" for i in range(counts["n"])]

        monkeypatch.setattr(SpaceKernelPlane, "_roster", _roster)
        monkeypatch.setattr(kp, "_kernel_name", lambda mid: f"k-{mid}")
        plane = SpaceKernelPlane(object(), state_root=str(tmp_path / "st"))
        await plane._ensure_runtime("s1")
        assert plane._fanout_warnings.get("s1")
        counts["n"] = 4
        await plane._ensure_runtime("s1")
        assert plane._fanout_warnings.get("s1") is None
