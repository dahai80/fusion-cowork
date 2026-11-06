"""issue #328: 审批拒绝修订回路 — 拒绝不再是终态失败.

on_reject=revise 的闸门/节点被拒时回到产出节点, 评审意见并入重跑输入;
超过 max_revisions 按拒绝终态处理; 未 opt-in 保持原 deny-fast 失败行为。
"""

from __future__ import annotations

import asyncio

import pytest

from fusion_cowork.engine import PermissionLevel, PermissionManager
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

GATE_NODE = "gate_328"


@register_node
class _ProducerNode(BaseNode):
    name = "producer_328"
    display_name = "Producer 328"
    category = NodeCategory.TOOL
    description = "测试用: 产出节点 — 记录每次收到的输入"
    icon = "📦"
    default_label = "Producer"

    runs: list = []

    async def execute(self, inputs: dict) -> NodeResult:
        type(self).runs.append(dict(inputs))
        return NodeResult(status=NodeStatus.SUCCESS, data={"draft": f"v{len(type(self).runs)}"}, summary="ok")


@register_node
class _GateNode(BaseNode):
    name = GATE_NODE
    display_name = "Gate 328"
    category = NodeCategory.TOOL
    description = "测试用: 闸门 (权限层 HITL 承载)"
    icon = "🚧"
    default_label = "Gate"

    async def execute(self, inputs: dict) -> NodeResult:
        return NodeResult(status=NodeStatus.SUCCESS, data={"approved": True}, summary="ok")


@pytest.fixture
def pm_328(monkeypatch):
    monkeypatch.delenv("FUSION_GUARD_ENABLED", raising=False)
    monkeypatch.delenv("FUSION_COWORK_HITL_PAUSE", raising=False)
    from fusion_cowork.security import guard as guard_mod

    monkeypatch.setattr(guard_mod, "guard_enabled", lambda: False)
    from fusion_cowork.engine import permission_manager

    monkeypatch.setattr(permission_manager, "HIGH_RISK_NODES", permission_manager.HIGH_RISK_NODES | {GATE_NODE})
    return PermissionManager(level=PermissionLevel.CONFIRM)


def _wf(gate_params: dict) -> Workflow:
    wf = Workflow(name="revise-328")
    n1 = NodeRegistry.create("producer_328")
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
    # 回退修订在同一事件循环片内完成 (重跑 + 再闸门之间不让出轮询窗口) —
    # _hitl_waiting 的 True/False 都可能被跳过, 用待审批队列条数作再闸门信号
    for _ in range(int(timeout * 50)):
        entries = [p for p in pm.list_pending() if p.get("execution_id") == exec_id]
        if len(entries) >= count:
            return True
        await asyncio.sleep(0.02)
    return False


class TestRevisionLoop:
    async def test_reject_returns_to_producer_with_comments(self, pm_328):
        _ProducerNode.runs = []
        eng = WorkflowEngine(permission_manager=pm_328, hitl_pause=True)
        exec_id = "run-328-loop"
        task = asyncio.create_task(
            eng.execute(_wf({"on_reject": "revise", "revision_node": "n1"}), execution_id=exec_id)
        )
        assert await _wait_hitl(eng, exec_id)
        eng.resolve_hitl(exec_id, False, comments="PRD 太薄, 补验收标准")
        assert await _wait_pending_count(pm_328, exec_id, 2), "修订重跑后应再次到达闸门"
        eng.resolve_hitl(exec_id, True, once=True)
        execution = await asyncio.wait_for(task, timeout=5)
        # 被闸步骤的 denied 记录留在时间线 — 终态按 #187 语义为 completed_with_denials
        assert execution.status.value == "completed_with_denials"
        # 产出节点重跑两次, 第二次输入带评审意见
        assert len(_ProducerNode.runs) == 2
        assert _ProducerNode.runs[1].get("revision_comments") == "PRD 太薄, 补验收标准"
        assert "revision_comments" not in _ProducerNode.runs[0]

    async def test_reject_without_optin_fails_run(self, pm_328):
        _ProducerNode.runs = []
        eng = WorkflowEngine(permission_manager=pm_328, hitl_pause=True)
        exec_id = "run-328-fail"
        task = asyncio.create_task(eng.execute(_wf({}), execution_id=exec_id))
        assert await _wait_hitl(eng, exec_id)
        eng.resolve_hitl(exec_id, False, comments="拒")
        execution = await asyncio.wait_for(task, timeout=5)
        assert execution.status.value == "failed"
        assert len(_ProducerNode.runs) == 1

    async def test_max_revisions_cap_fails_run(self, pm_328):
        _ProducerNode.runs = []
        eng = WorkflowEngine(permission_manager=pm_328, hitl_pause=True)
        exec_id = "run-328-cap"
        task = asyncio.create_task(
            eng.execute(_wf({"on_reject": "revise", "revision_node": "n1", "max_revisions": 1}), execution_id=exec_id)
        )
        assert await _wait_hitl(eng, exec_id)
        eng.resolve_hitl(exec_id, False)  # 第 1 次拒绝 → 修订
        assert await _wait_pending_count(pm_328, exec_id, 2), "修订重跑后应再次到达闸门"
        eng.resolve_hitl(exec_id, False)  # 第 2 次拒绝 → 超上限, 终态失败
        execution = await asyncio.wait_for(task, timeout=5)
        assert execution.status.value == "failed"
        assert len(_ProducerNode.runs) == 2

    async def test_revision_node_auto_detect_upstream(self, pm_328):
        _ProducerNode.runs = []
        eng = WorkflowEngine(permission_manager=pm_328, hitl_pause=True)
        exec_id = "run-328-auto"
        task = asyncio.create_task(eng.execute(_wf({"on_reject": "revise"}), execution_id=exec_id))
        assert await _wait_hitl(eng, exec_id)
        eng.resolve_hitl(exec_id, False, comments="改")
        assert await _wait_pending_count(pm_328, exec_id, 2)
        eng.resolve_hitl(exec_id, True, once=True)
        execution = await asyncio.wait_for(task, timeout=5)
        assert execution.status.value == "completed_with_denials"
        assert len(_ProducerNode.runs) == 2

    async def test_self_revision_for_non_gate_node(self, pm_328, monkeypatch):
        # 非闸门 HIGH_RISK 节点自身被拒 — on_reject=revise 回到自身重跑
        from fusion_cowork.engine import permission_manager

        monkeypatch.setattr(
            permission_manager, "HIGH_RISK_NODES", permission_manager.HIGH_RISK_NODES | {"producer_328"}
        )
        _ProducerNode.runs = []
        eng = WorkflowEngine(permission_manager=pm_328, hitl_pause=True)
        exec_id = "run-328-self"
        wf = Workflow(name="revise-328-self")
        n1 = NodeRegistry.create("producer_328")
        n1.id = "n1"
        n1.config = NodeConfig(params={"on_reject": "revise"})
        wf.add_node(n1)
        task = asyncio.create_task(eng.execute(wf, execution_id=exec_id))
        assert await _wait_hitl(eng, exec_id)
        eng.resolve_hitl(exec_id, False, comments="重写")
        assert await _wait_pending_count(pm_328, exec_id, 2)
        eng.resolve_hitl(exec_id, True, once=True)
        execution = await asyncio.wait_for(task, timeout=5)
        assert execution.status.value == "completed_with_denials"
        # 自身被拒发生在 execute 之前 — 首次尝试无产出记录, 修订重跑 (批准后) 只执行一次
        assert len(_ProducerNode.runs) == 1
        assert _ProducerNode.runs[0].get("revision_comments") == "重写"
