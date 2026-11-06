"""issue #326: 审批卡上游产物证据 — 待审批载荷附 artifacts 块.

被闸节点的直接上游产出快照 (node_id/node_name/output) 进 pending 详情,
评审者不用离开审批界面去找产出资材; 证据仅快照, 不改节点输入。
"""

from __future__ import annotations

import asyncio

import pytest

from fusion_cowork.engine import PermissionLevel, PermissionManager
from fusion_cowork.engine.node import (
    BaseNode,
    NodeCategory,
    NodeRegistry,
    NodeResult,
    NodeStatus,
    register_node,
)
from fusion_cowork.engine.workflow import Workflow, WorkflowEngine

GATE_NODE = "gate_326"


@register_node
class _ProducerNode(BaseNode):
    name = "producer_326"
    display_name = "BA 产出"
    category = NodeCategory.TOOL
    description = "测试用: 产出节点 — 输出大文档"
    icon = "📦"
    default_label = "Producer"

    async def execute(self, inputs: dict) -> NodeResult:
        return NodeResult(
            status=NodeStatus.SUCCESS,
            data={"report": "PRD-BODY-" + "x" * 5000, "score": 42, "tags": ["a", "b"]},
            summary="ok",
        )


@register_node
class _GateNode(BaseNode):
    name = GATE_NODE
    display_name = "Gate 326"
    category = NodeCategory.TOOL
    description = "测试用: 闸门"
    icon = "🚧"
    default_label = "Gate"

    async def execute(self, inputs: dict) -> NodeResult:
        return NodeResult(status=NodeStatus.SUCCESS, data={"approved": True}, summary="ok")


@pytest.fixture
def pm_326(monkeypatch):
    monkeypatch.delenv("FUSION_GUARD_ENABLED", raising=False)
    monkeypatch.delenv("FUSION_COWORK_HITL_PAUSE", raising=False)
    from fusion_cowork.security import guard as guard_mod

    monkeypatch.setattr(guard_mod, "guard_enabled", lambda: False)
    from fusion_cowork.engine import permission_manager

    monkeypatch.setattr(permission_manager, "HIGH_RISK_NODES", permission_manager.HIGH_RISK_NODES | {GATE_NODE})
    return PermissionManager(level=PermissionLevel.CONFIRM)


def _wf() -> Workflow:
    wf = Workflow(name="evidence-326")
    n1 = NodeRegistry.create("producer_326")
    n1.id = "n1"
    n2 = NodeRegistry.create(GATE_NODE)
    n2.id = "n2"
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


class TestApprovalEvidence:
    async def test_pending_entry_carries_upstream_artifacts(self, pm_326):
        eng = WorkflowEngine(permission_manager=pm_326, hitl_pause=True)
        exec_id = "run-326-ev"
        task = asyncio.create_task(eng.execute(_wf(), execution_id=exec_id))
        assert await _wait_hitl(eng, exec_id)
        pending = [p for p in pm_326.list_pending() if p.get("execution_id") == exec_id]
        assert len(pending) == 1
        arts = pending[0].get("artifacts")
        assert isinstance(arts, list) and len(arts) == 1
        ev = arts[0]
        assert ev["node_id"] == "n1"
        assert ev["node_name"] == "BA 产出"
        out = ev["output"]
        assert out["score"] == 42
        # 非 str 标量直传, dict/list JSON 编码截断
        assert out["tags"] == '["a", "b"]'
        # 大字符串截断到 2000 字 — 预览用途, 不承载全量产出
        assert isinstance(out["report"], str) and len(out["report"]) == 2000
        assert out["report"].startswith("PRD-BODY-")
        eng.resolve_hitl(exec_id, True, once=True)
        execution = await asyncio.wait_for(task, timeout=5)
        assert execution.status.value == "success"

    async def test_evidence_is_snapshot_not_node_input(self, pm_326):
        # 证据只进审批卡快照 — 被闸节点输入不含 artifacts/上游污染
        eng = WorkflowEngine(permission_manager=pm_326, hitl_pause=True)
        exec_id = "run-326-snap"
        task = asyncio.create_task(eng.execute(_wf(), execution_id=exec_id))
        assert await _wait_hitl(eng, exec_id)
        eng.resolve_hitl(exec_id, True, once=True)
        execution = await asyncio.wait_for(task, timeout=5)
        assert execution.status.value == "success"
        gate_step = next(s for s in execution.steps if s.node_id == "n2")
        # report 经边数据流正常传入 (设计如此); 证据块本身不进节点输入
        assert "artifacts" not in (gate_step.input_data or {})
        assert not any(k.startswith("evidence") for k in (gate_step.input_data or {}))

    async def test_no_upstream_empty_artifacts(self, pm_326, monkeypatch):
        # 无上游的被闸节点 — artifacts 为空列表, 卡片不渲染证据块
        from fusion_cowork.engine import permission_manager

        monkeypatch.setattr(
            permission_manager, "HIGH_RISK_NODES", permission_manager.HIGH_RISK_NODES | {"producer_326"}
        )
        wf = Workflow(name="evidence-326-solo")
        n1 = NodeRegistry.create("producer_326")
        n1.id = "n1"
        wf.add_node(n1)
        eng = WorkflowEngine(permission_manager=pm_326, hitl_pause=True)
        exec_id = "run-326-solo"
        task = asyncio.create_task(eng.execute(wf, execution_id=exec_id))
        assert await _wait_hitl(eng, exec_id)
        pending = [p for p in pm_326.list_pending() if p.get("execution_id") == exec_id]
        assert len(pending) == 1
        assert pending[0].get("artifacts") == []
        eng.resolve_hitl(exec_id, True, once=True)
        execution = await asyncio.wait_for(task, timeout=5)
        assert execution.status.value == "success"

    async def test_evidence_cleared_after_check(self, pm_326):
        # 证据 ContextVar 在 check 后复位 — 后续无关节点的待审批不带旧证据
        from fusion_cowork.engine.permission_manager import _approval_evidence

        eng = WorkflowEngine(permission_manager=pm_326, hitl_pause=True)
        exec_id = "run-326-clear"
        task = asyncio.create_task(eng.execute(_wf(), execution_id=exec_id))
        assert await _wait_hitl(eng, exec_id)
        assert _approval_evidence.get([]) == []
        eng.resolve_hitl(exec_id, True, once=True)
        await asyncio.wait_for(task, timeout=5)
