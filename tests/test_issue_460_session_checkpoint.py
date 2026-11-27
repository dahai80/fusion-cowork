"""issue #460: session-level checkpoints — step snapshots carry session_id, resume adopts the original session.

Agent seat nodes (studio_run etc.) keep multi-turn conversation history in the SessionStore; once
step snapshots carry a session_id, resume reuses the original session instead of creating a new
one, and downstream node inputs get _session_id injected.
Old snapshots (without session_id) keep unchanged resume behavior.
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
        # Session steps_snapshot attributes in sync
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
        # Simulate a failure at n2: snapshot contains only n1
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
        # New engine (simulated restart) + same session store
        eng2 = WorkflowEngine(permission_manager=PermissionManager(), session_store=SessionStore(db))
        exec2 = await eng2.execute(_wf_two_nodes(), initial_input={"seed": "a"}, resume_steps=snap)
        assert exec2.status.value == "success"
        # Session adopted: new execution steps share one session_id equal to the original session
        sids = {s.session_id for s in exec2.steps}
        assert sids == {old_sid}, sids
        # n2 (probe460) input received _session_id = the original session id
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
        # Old snapshot without session_id → a new session is created, id differs from the original
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
