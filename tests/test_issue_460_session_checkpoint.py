"""issue #460: 会话级检查点 — 步快照携带 session_id, resume 采纳原会话。

agent 座位节点 (studio_run 等) 多轮对话历史存于 SessionStore; 步快照带
session_id 后, resume 不新建会话而是续用原会话, 下游节点输入注入 _session_id。
旧快照 (无 session_id) resume 行为不变。
"""

from __future__ import annotations

from fusion_cowork.engine import PermissionManager, WorkflowEngine
from fusion_cowork.engine.node import BaseNode, NodeCategory, NodeResult, NodeStatus, register_node
from fusion_cowork.engine.session import SessionStore
from fusion_cowork.engine.workflow import Workflow, WorkflowStep


@register_node
class _Probe460(BaseNode):
    name = "probe460"
    display_name = "Probe 460"
    category = NodeCategory.LOGIC
    description = "test probe: exposes session id from inputs (#460)"
    icon = "🧪"
    default_label = "Probe"

    async def execute(self, inputs: dict) -> NodeResult:
        return NodeResult(
            status=NodeStatus.SUCCESS,
            data={"sid": str(inputs.get("_session_id", "")), "seen": str(inputs.get("seed", ""))},
            summary="probe",
        )


@register_node
class _Echo460(BaseNode):
    name = "echo460"
    display_name = "Echo 460"
    category = NodeCategory.LOGIC
    description = "test probe: passes seed through (#460)"
    icon = "🧪"
    default_label = "Echo"

    async def execute(self, inputs: dict) -> NodeResult:
        return NodeResult(status=NodeStatus.SUCCESS, data={"seed": str(inputs.get("seed", ""))}, summary="echo")


def _engine(tmp_path):
    return WorkflowEngine(permission_manager=PermissionManager(), session_store=SessionStore(str(tmp_path / "s.db")))


def _wf_two_nodes():
    return Workflow.from_dict(
        {
            "name": "cp-wf",
            "nodes": [
                {"id": "n1", "name": "echo460", "config": {}},
                {"id": "n2", "name": "probe460", "config": {}},
            ],
            "edges": [{"source_id": "n1", "target_id": "n2"}],
        }
    )


class TestStepSessionField:
    def test_default_none(self):
        s = WorkflowStep(node_id="a", node_name="x", node_display_name="x", status=NodeStatus.SUCCESS)
        assert s.session_id is None

    async def test_steps_stamped_with_session(self, tmp_path):
        eng = _engine(tmp_path)
        exec_result = await eng.execute(_wf_two_nodes(), initial_input={"seed": "s1"})
        assert exec_result.status.value == "success"
        sids = {s.session_id for s in exec_result.steps}
        assert len(sids) == 1 and all(sids), sids
        # 会话 steps_snapshot 同步归因
        sess = eng._session_store.get(next(iter(sids)))
        assert sess is not None
        assert all(st.get("session_id") == next(iter(sids)) for st in sess.steps_snapshot)

    async def test_no_store_no_stamp(self):
        eng = WorkflowEngine(permission_manager=PermissionManager())
        exec_result = await eng.execute(_wf_two_nodes(), initial_input={"seed": "s"})
        assert all(s.session_id is None for s in exec_result.steps)


class TestResumeAdoption:
    async def test_resume_adopts_session(self, tmp_path):
        db = str(tmp_path / "s.db")
        eng1 = WorkflowEngine(permission_manager=PermissionManager(), session_store=SessionStore(db))
        exec1 = await eng1.execute(_wf_two_nodes(), initial_input={"seed": "a"})
        # 模拟 n2 处失败: 快照只含 n1
        snap = [
            {
                "node_id": s.node_id,
                "node_name": s.node_name,
                "status": s.status.value,
                "output_data": s.output_data,
                "session_id": s.session_id,
            }
            for s in exec1.steps
            if s.node_id == "n1"
        ]
        old_sid = exec1.steps[0].session_id
        # 新引擎 (模拟重启) + 同一 session 库
        eng2 = WorkflowEngine(permission_manager=PermissionManager(), session_store=SessionStore(db))
        exec2 = await eng2.execute(_wf_two_nodes(), initial_input={"seed": "a"}, resume_steps=snap)
        assert exec2.status.value == "success"
        # 会话被采纳: 新执行步骤同一 session_id 且等于原会话
        sids = {s.session_id for s in exec2.steps}
        assert sids == {old_sid}, sids
        # n2 (probe460) 输入收到 _session_id = 原会话 id
        n2 = next(s for s in exec2.steps if s.node_id == "n2")
        assert n2.output_data.get("sid") == old_sid, n2.output_data

    async def test_resume_without_session_ref_unchanged(self, tmp_path):
        db = str(tmp_path / "s.db")
        eng1 = WorkflowEngine(permission_manager=PermissionManager(), session_store=SessionStore(db))
        exec1 = await eng1.execute(_wf_two_nodes(), initial_input={"seed": "b"})
        snap = [
            {"node_id": s.node_id, "node_name": s.node_name, "status": s.status.value, "output_data": s.output_data}
            for s in exec1.steps
        ]
        eng2 = WorkflowEngine(permission_manager=PermissionManager(), session_store=SessionStore(db))
        exec2 = await eng2.execute(_wf_two_nodes(), initial_input={"seed": "b"}, resume_steps=snap)
        assert exec2.status.value == "success"
        # 旧快照无 session_id → 新建会话, id 与原不同
        new_sids = {s.session_id for s in exec2.steps}
        assert len(new_sids) == 1
        assert new_sids != {exec1.steps[0].session_id}

    async def test_resume_missing_session_falls_back_to_new(self, tmp_path):
        db = str(tmp_path / "s.db")
        eng = WorkflowEngine(permission_manager=PermissionManager(), session_store=SessionStore(db))
        snap = [
            {
                "node_id": "n1",
                "node_name": "echo460",
                "status": "success",
                "output_data": {"seed": "c"},
                "session_id": "sess_ghost",
            }
        ]
        exec2 = await eng.execute(_wf_two_nodes(), initial_input={"seed": "c"}, resume_steps=snap)
        assert exec2.status.value == "success"
        sids = {s.session_id for s in exec2.steps}
        assert len(sids) == 1 and next(iter(sids)) != "sess_ghost"
