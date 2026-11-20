"""issue #563: kernel vocabulary wiring (AWAITING_HUMAN_MARKER re-delivery,
model_tier validation, node-level fan-out pre-validation).

Item 1: pending HumanGate questions survive a crash on disk; after restart
`hitl_recover` surfaces them read-only (checkpoint marker verified) and the
resume path REBINDS the original question instead of asking a duplicate.

Item 2 (#141): workflow_ladder workers carry an optional `model_tier` hint,
validated against kernel VALID_MODEL_TIERS before any dispatch (typed error).

Item 3 (#124): node-level fan-out pre-validation — above the complex band the
kernel token-multiplier warning rides the stage report; above the hard max the
node fails with a typed error before dispatch.
"""

from __future__ import annotations

import asyncio

import pytest
from fusion_harness import AWAITING_HUMAN_MARKER, TOKEN_MULTIPLIER_WARNING, HumanGate

from fusion_cowork.engine import PermissionLevel, PermissionManager
from fusion_cowork.engine.kernel_checkpoint import KernelCheckpointBinder
from fusion_cowork.engine.node import NodeConfig, NodeRegistry
from fusion_cowork.engine.workflow import WorkflowEngine

HITL_NODE = "hitl_probe_563"


@pytest.fixture
def pm_no_guard(monkeypatch):
    monkeypatch.delenv("FUSION_GUARD_ENABLED", raising=False)
    monkeypatch.delenv("FUSION_COWORK_HITL_PAUSE", raising=False)
    monkeypatch.delenv("FUSION_COWORK_HITL_TIMEOUT", raising=False)
    from fusion_cowork.engine import permission_manager

    monkeypatch.setattr(permission_manager, "HIGH_RISK_NODES", permission_manager.HIGH_RISK_NODES | {HITL_NODE})
    return PermissionManager(level=PermissionLevel.CONFIRM)


def _wf():
    from fusion_cowork.engine.workflow import Workflow

    wf = Workflow(name="hitl-wf-563")
    wf.add_node(NodeRegistry.create(HITL_NODE))
    return wf


async def _wait_hitl(engine: WorkflowEngine, exec_id: str, timeout: float = 5.0) -> bool:
    for _ in range(int(timeout * 50)):
        if engine.hitl_pending(exec_id):
            return True
        await asyncio.sleep(0.02)
    return False


def _register_probe():
    from fusion_cowork.engine.node import BaseNode, NodeCategory, NodeResult, NodeStatus, register_node

    if NodeRegistry.get(HITL_NODE) is None:

        @register_node
        class _Probe563(BaseNode):
            name = HITL_NODE
            display_name = "HITL Probe 563"
            category = NodeCategory.TOOL
            description = "test-only high-risk probe (#563)"
            icon = "🧪"
            default_label = "Probe"

            async def execute(self, inputs: dict) -> NodeResult:
                return NodeResult(status=NodeStatus.SUCCESS, data={"ok": True}, summary="ok")


class TestItem1MarkerRedelivery:
    async def test_sigkill_restart_redelivers_question(self, tmp_path, pm_no_guard, monkeypatch):
        # SIGKILL simulation: engine 1 parks mid-HITL; a fresh engine (same
        # gate dir + checkpoint db) recovers the pending question read-only
        _register_probe()
        monkeypatch.setenv("FUSION_COWORK_HITL_GATE_DIR", str(tmp_path / "gate"))
        db = str(tmp_path / "ck.db")
        eng1 = WorkflowEngine(permission_manager=pm_no_guard, kernel_checkpoints=KernelCheckpointBinder(db_path=db))
        eng1._hitl_gate = None
        exec_id = "run-563-crash"
        task = asyncio.create_task(eng1.execute(_wf(), execution_id=exec_id))
        assert await _wait_hitl(eng1, exec_id)
        # engine 1 "dies" — no resolve, task abandoned; fresh engine takes over
        eng2 = WorkflowEngine(permission_manager=pm_no_guard, kernel_checkpoints=KernelCheckpointBinder(db_path=db))
        eng2._hitl_gate = None
        info = eng2.hitl_recover(exec_id)
        assert info is not None
        assert info["question_id"]
        assert info["node_id"]
        assert info["policy"] == "block"
        # checkpoint marker verified against the kernel store
        assert info["marker_verified"] is True
        state = eng2._kernel_checkpoints.store.latest(exec_id)
        assert state.permission_mode == AWAITING_HUMAN_MARKER
        eng1._hitl_gate_cancel(exec_id)
        task.cancel()

    async def test_recover_none_when_no_pending(self, tmp_path, pm_no_guard, monkeypatch):
        monkeypatch.setenv("FUSION_COWORK_HITL_GATE_DIR", str(tmp_path / "gate"))
        eng = WorkflowEngine(permission_manager=pm_no_guard, kernel_checkpoints=None)
        assert eng.hitl_recover("run-563-nothing") is None

    async def test_recover_unwired_gate_returns_none(self, monkeypatch, tmp_path):
        monkeypatch.delenv("FUSION_COWORK_HITL_GATE_DIR", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        eng = WorkflowEngine(permission_manager=PermissionManager(level=PermissionLevel.CONFIRM))
        # fresh gate dir under the temp HOME — empty queue
        assert eng.hitl_recover("run-563-empty") is None

    async def test_resume_reuses_pending_question(self, tmp_path, monkeypatch):
        # resume path: an existing pending question for the same node is
        # REBOUND (no duplicate ask) — resolve against the original wakes the run
        gate_dir = tmp_path / "gate"
        gate = HumanGate(str(gate_dir))
        q = gate.ask(
            session_id="run-563-resume",
            question="awaiting approval",
            payload={"node_id": "n1", "tool_name": "shell_exec"},
            policy="block",
        )
        eng = WorkflowEngine(permission_manager=PermissionManager(level=PermissionLevel.CONFIRM))
        eng._hitl_gate = gate
        stub = type("_StubNode", (), {"id": "n1"})()
        assert eng._hitl_gate_reuse("run-563-resume", stub) == q.id
        assert eng._hitl_questions["run-563-resume"] == q.id
        gate.cancel(q.id)

    async def test_reuse_no_payload_match_asks_fresh(self, tmp_path):
        gate = HumanGate(str(tmp_path / "gate2"))
        q = gate.ask(
            session_id="run-563-mismatch",
            question="awaiting approval",
            payload={"node_id": "other", "tool_name": "x"},
            policy="block",
        )
        eng = WorkflowEngine(permission_manager=PermissionManager(level=PermissionLevel.CONFIRM))
        eng._hitl_gate = gate
        stub = type("_StubNode", (), {"id": "n1"})()
        assert eng._hitl_gate_reuse("run-563-mismatch", stub) == ""
        gate.cancel(q.id)


def _make_ladder(params: dict):
    import fusion_cowork.nodes.ecosystem.ladder_node  # noqa: F401

    cls = NodeRegistry.get("workflow_ladder")
    return cls(config=NodeConfig(params=params))


class TestItem2ModelTier:
    async def test_invalid_tier_fails_before_dispatch(self):
        node = _make_ladder(
            {"stages": [{"name": "s1", "workers": [{"name": "a", "prompt": "p", "model_tier": "huge"}]}]}
        )
        res = await node.execute({})
        assert res.status.value == "failed"
        assert "unknown model tier" in res.error
        assert "huge" in res.error

    async def test_invalid_tier_in_later_stage_still_fails(self):
        # upfront validation: stage 2's bad tier fails the node even though
        # stage 1 would have dispatched fine
        node = _make_ladder(
            {
                "stages": [
                    {"name": "s1", "workers": [{"name": "a", "prompt": "p"}]},
                    {"name": "s2", "workers": [{"name": "b", "prompt": "p", "model_tier": "ultra"}]},
                ]
            }
        )
        res = await node.execute({})
        assert res.status.value == "failed"
        assert "unknown model tier" in res.error

    async def test_valid_tier_runs_and_reported(self):
        node = _make_ladder(
            {
                "stages": [
                    {
                        "name": "s1",
                        "workers": [
                            {"name": "a", "prompt": "p1", "model_tier": "cheap"},
                            {"name": "b", "prompt": "p2", "model_tier": "strong"},
                        ],
                    }
                ]
            }
        )
        res = await node.execute({})
        assert res.status.value == "success"
        tiers = {r["name"]: r["model_tier"] for r in res.data["stages"][0]["results"]}
        assert tiers == {"a": "cheap", "b": "strong"}

    async def test_empty_tier_is_default(self):
        node = _make_ladder({"stages": [{"name": "s1", "workers": [{"name": "a", "prompt": "p"}]}]})
        res = await node.execute({})
        assert res.status.value == "success"
        assert res.data["stages"][0]["results"][0]["model_tier"] == ""


class TestItem3FanoutPrevalidation:
    @staticmethod
    def _workers(n: int, **kw):
        return [{"name": f"w{i}", "prompt": "p", **kw} for i in range(n)]

    async def test_hard_max_rejected_before_dispatch(self):
        node = _make_ladder({"stages": [{"name": "s1", "workers": self._workers(21)}]})
        res = await node.execute({})
        assert res.status.value == "failed"
        assert "hard max 20" in res.error

    async def test_complex_band_warning_in_result(self):
        node = _make_ladder({"stages": [{"name": "s1", "workers": self._workers(16)}]})
        res = await node.execute({})
        assert res.status.value == "success"
        warnings = res.data["stages"][0]["warnings"]
        assert warnings
        assert TOKEN_MULTIPLIER_WARNING in warnings[0]

    async def test_in_band_no_warning(self):
        node = _make_ladder({"stages": [{"name": "s1", "workers": self._workers(5)}]})
        res = await node.execute({})
        assert res.status.value == "success"
        assert res.data["stages"][0]["warnings"] == []

    async def test_hard_max_in_later_stage_fails_upfront(self):
        node = _make_ladder(
            {
                "stages": [
                    {"name": "s1", "workers": self._workers(2)},
                    {"name": "s2", "workers": self._workers(25)},
                ]
            }
        )
        res = await node.execute({})
        assert res.status.value == "failed"
        assert "hard max 20" in res.error
