"""issue #527: HITL 审批闸门接入共享内核 HumanGate 契约 (fusion-harness #66)。

park 点 = ask(policy="block", ttl_s) + wait(); 审批侧 = resolve() 单赢家;
ttl 到期惰性过期; 审批路由策略 (guard/闸门工具/journal/once) 仍全部在 cowork。
"""

from __future__ import annotations

import asyncio
import time

import pytest
from fusion_harness import HumanGate

from fusion_cowork.engine import PermissionLevel, PermissionManager
from fusion_cowork.engine.node import BaseNode, NodeCategory, NodeRegistry, NodeResult, NodeStatus, register_node
from fusion_cowork.engine.workflow import Workflow, WorkflowEngine, WorkflowStatus

HITL_NODE = "hitl_probe_527"


@register_node
class _ProbeNode(BaseNode):
    name = HITL_NODE
    display_name = "HITL Probe 527"
    category = NodeCategory.TOOL
    description = "test-only high-risk probe (#527)"
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


@pytest.fixture
def gate(tmp_path):
    return HumanGate(str(tmp_path / "gate"))


@pytest.fixture
def eng(pm_no_guard, gate):
    return WorkflowEngine(permission_manager=pm_no_guard, hitl_gate=gate)


def _wf():
    wf = Workflow(name="hitl-wf-527")
    wf.add_node(NodeRegistry.create(HITL_NODE))
    return wf


async def _wait_hitl(engine: WorkflowEngine, exec_id: str, timeout: float = 5.0) -> bool:
    for _ in range(int(timeout * 50)):
        if engine.hitl_pending(exec_id):
            return True
        await asyncio.sleep(0.02)
    return False


async def _wait_question(engine: WorkflowEngine, gate: HumanGate, exec_id: str, timeout: float = 5.0) -> str:
    for _ in range(int(timeout * 50)):
        qid = engine._hitl_questions.get(exec_id)
        if qid:
            return qid
        await asyncio.sleep(0.02)
    return ""


class TestParkPoint:
    async def test_park_queues_kernel_block_question(self, eng, gate):
        _ProbeNode.calls = 0
        exec_id = "run-527-park"
        task = asyncio.create_task(eng.execute(_wf(), execution_id=exec_id))
        assert await _wait_hitl(eng, exec_id)
        qid = await _wait_question(eng, gate, exec_id)
        assert qid, "park must queue a kernel HumanGate question"
        pending = gate.pending(session_id=exec_id)
        assert len(pending) == 1
        q = pending[0]
        assert q.id == qid
        assert q.policy == "block"
        assert q.requester == "workflow_engine"
        assert q.expires_at > 0 and q.expires_at > time.time()
        assert q.payload["node_id"]
        assert q.payload["tool_name"] == HITL_NODE
        assert not eng._cancel_flags.get(exec_id)
        # 收尾: 批准放行 (一次性 — 自定义高风险节点无持久规则, 复查必拒)
        assert eng.resolve_hitl(exec_id, True, once=True)
        execution = await asyncio.wait_for(task, timeout=5)
        assert execution.status == WorkflowStatus.SUCCESS
        assert _ProbeNode.calls == 1

    async def test_pause_off_never_queues_question(self, pm_no_guard, gate):
        eng = WorkflowEngine(permission_manager=pm_no_guard, hitl_pause=False, hitl_gate=gate)
        execution = await eng.execute(_wf(), execution_id="run-527-off")
        assert execution.status == WorkflowStatus.FAILED
        assert gate.pending() == []
        assert eng._hitl_questions == {}


class TestApproverSide:
    async def test_resolve_routes_through_kernel_single_winner(self, eng, gate):
        _ProbeNode.calls = 0
        exec_id = "run-527-resolve"
        task = asyncio.create_task(eng.execute(_wf(), execution_id=exec_id))
        assert await _wait_hitl(eng, exec_id)
        qid = await _wait_question(eng, gate, exec_id)
        # 第一次决定生效 — 内核问题被 answer, 回复存档
        assert eng.resolve_hitl(exec_id, True, once=True) is True
        answered = [q for q in gate.list_questions() if q.id == qid]
        assert len(answered) == 1 and answered[0].status == "answered"
        assert answered[0].reply["approved"] is True
        assert answered[0].reply["once"] is True
        # 单赢家: 第二个 resolve 被内核拒绝, 决定不被覆盖 (不翻盘成拒绝)
        assert eng.resolve_hitl(exec_id, False) is False
        execution = await asyncio.wait_for(task, timeout=5)
        assert execution.status == WorkflowStatus.SUCCESS
        assert _ProbeNode.calls == 1

    async def test_kernel_rejects_second_winner_before_decision(self, eng, gate):
        _ProbeNode.calls = 0
        exec_id = "run-527-dup"
        task = asyncio.create_task(eng.execute(_wf(), execution_id=exec_id))
        assert await _wait_hitl(eng, exec_id)
        qid = await _wait_question(eng, gate, exec_id)
        # 内核层直接 resolve 占坑 — 引擎侧 resolve_hitl 必须被单赢家拒绝;
        # 引擎采纳内核 answer (approved+once) 继续跑
        res = gate.resolve(qid, {"approved": True, "once": True})
        assert res["resolved"] is True
        assert eng.resolve_hitl(exec_id, False) is False
        execution = await asyncio.wait_for(task, timeout=5)
        assert execution.status == WorkflowStatus.SUCCESS


class TestExpiry:
    async def test_ttl_expiry_auto_deny_and_kernel_expired(self, pm_no_guard, gate, monkeypatch):
        _ProbeNode.calls = 0
        monkeypatch.setenv("FUSION_COWORK_HITL_TIMEOUT", "1")
        eng = WorkflowEngine(permission_manager=pm_no_guard, hitl_gate=gate)
        exec_id = "run-527-expire"
        task = asyncio.create_task(eng.execute(_wf(), execution_id=exec_id))
        assert await _wait_hitl(eng, exec_id)
        qid = await _wait_question(eng, gate, exec_id)
        q = gate.pending(session_id=exec_id)[0]
        assert 0 < q.expires_at - q.created_at <= 1.5, "ttl_s must carry the engine deadline"
        execution = await asyncio.wait_for(task, timeout=5)
        assert execution.status == WorkflowStatus.FAILED
        assert _ProbeNode.calls == 0
        # ttl 到期惰性过期 — 内核队列不留 pending
        statuses = {x.id: x.status for x in gate.list_questions()}
        assert statuses[qid] == "expired"
        assert gate.pending() == []


class TestW8Bridges:
    async def test_resume_cancels_question_and_reasks(self, eng, gate):
        exec_id = "run-527-resume"
        task = asyncio.create_task(eng.execute(_wf(), execution_id=exec_id))
        assert await _wait_hitl(eng, exec_id)
        qid1 = await _wait_question(eng, gate, exec_id)
        assert eng.resume(exec_id) is True
        # resume 不合成决定 — 内核问题被取消并重问, 继续等
        for _ in range(250):
            qid2 = eng._hitl_questions.get(exec_id)
            if qid2 and qid2 != qid1:
                break
            await asyncio.sleep(0.02)
        else:
            qid2 = ""
        assert qid2, "resume must re-ask a fresh kernel question"
        statuses = {x.id: x.status for x in gate.list_questions()}
        assert statuses[qid1] == "cancelled"
        assert eng.hitl_pending(exec_id)
        # 决定照常生效 (一次性批准 — 无持久规则)
        assert eng.resolve_hitl(exec_id, True, once=True) is True
        execution = await asyncio.wait_for(task, timeout=5)
        assert execution.status == WorkflowStatus.SUCCESS

    async def test_cancel_cancels_kernel_question(self, eng, gate):
        exec_id = "run-527-cancel"
        task = asyncio.create_task(eng.execute(_wf(), execution_id=exec_id))
        assert await _wait_hitl(eng, exec_id)
        qid = await _wait_question(eng, gate, exec_id)
        assert eng.cancel(exec_id) is True
        execution = await asyncio.wait_for(task, timeout=5)
        assert execution.status == WorkflowStatus.FAILED
        statuses = {x.id: x.status for x in gate.list_questions()}
        assert statuses[qid] == "cancelled"
        assert gate.pending() == []
        assert exec_id not in eng._hitl_questions


class TestPersistence:
    async def test_gate_state_file_persisted(self, pm_no_guard, tmp_path):
        gate_dir = tmp_path / "gate-persist"
        eng = WorkflowEngine(permission_manager=pm_no_guard, hitl_gate=HumanGate(str(gate_dir)))
        exec_id = "run-527-persist"
        task = asyncio.create_task(eng.execute(_wf(), execution_id=exec_id))
        assert await _wait_hitl(eng, exec_id)
        assert (gate_dir / "human_gate_queue.json").exists()
        eng.cancel(exec_id)
        await asyncio.wait_for(task, timeout=5)
        # 重建 gate — 队列从盘恢复, 无 pending 残留
        gate2 = HumanGate(str(gate_dir))
        assert gate2.pending() == []
