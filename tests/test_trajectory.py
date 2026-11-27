"""D1 trajectory flywheel — TrajectoryRecorder unit tests (issue #520 kernel schema)."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from fusion_cowork.engine.hooks import HookEvent, HookManager
from fusion_cowork.engine.session import Session, SessionStore
from fusion_cowork.space.models import SpaceMessage
from fusion_cowork.space.store import SpaceStore
from fusion_cowork.trajectory.recorder import (
    TrajectoryRecorder,
    TrajectoryWriter,
    kernel_record,
)
from fusion_cowork.trajectory.space import SpaceTrajectoryExporter


@pytest.fixture
def tmp_trajectory_dir():
    with tempfile.TemporaryDirectory() as d:
        yield d


@pytest.fixture
def tmp_session_store():
    with tempfile.TemporaryDirectory() as d:
        yield SessionStore(str(Path(d) / "sessions.db"))


class TestTrajectoryWriter:
    def test_write_creates_jsonl(self, tmp_trajectory_dir):
        writer = TrajectoryWriter(tmp_trajectory_dir)
        evt = kernel_record("workflow_start", run_id="exec_1", payload={"session_id": "sess_1"})
        path = writer.write(evt)
        assert Path(path).exists()
        line = Path(path).read_text(encoding="utf-8").strip()
        data = json.loads(line)
        # kernel normative shape: run_id/event/payload, semantic event preserved
        assert data["run_id"] == "exec_1"
        assert data["event"] == "start"
        assert data["payload"]["legacy_event"] == "workflow_start"
        assert data["payload"]["session_id"] == "sess_1"

    def test_write_appends_same_session(self, tmp_trajectory_dir):
        writer = TrajectoryWriter(tmp_trajectory_dir)
        writer.write(kernel_record("workflow_start", run_id="e1", payload={"session_id": "sess_1"}))
        writer.write(kernel_record("workflow_end", run_id="e1", payload={"session_id": "sess_1"}))
        lines = Path(tmp_trajectory_dir, "sess_1.jsonl").read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 2

    def test_no_session_uses_default_bucket(self, tmp_trajectory_dir):
        writer = TrajectoryWriter(tmp_trajectory_dir)
        writer.write(kernel_record("session_start", run_id="e1"))
        assert Path(tmp_trajectory_dir, "no_session.jsonl").exists()

    def test_list_trajectories(self, tmp_trajectory_dir):
        writer = TrajectoryWriter(tmp_trajectory_dir)
        writer.write(kernel_record("workflow_start", run_id="e1", payload={"session_id": "a"}))
        writer.write(kernel_record("workflow_start", run_id="e2", payload={"session_id": "b"}))
        names = writer.list_trajectories()
        assert "a.jsonl" in names and "b.jsonl" in names

    def test_kernel_roundtrip(self, tmp_trajectory_dir):
        # issue #520 acceptance: a cowork-written trajectory loads with kernel tooling
        from fusion_harness.trajectory import TrajectoryReader

        writer = TrajectoryWriter(tmp_trajectory_dir)
        writer.write(kernel_record("post_node_execute", run_id="e1", node_id="n1", payload={"session_id": "rt"}))
        writer.write(kernel_record("node_error", run_id="e1", node_id="n2", payload={"session_id": "rt"}))
        records = TrajectoryReader(str(Path(tmp_trajectory_dir, "rt.jsonl"))).read_all()
        assert [r.event for r in records] == ["tool_result", "error"]
        assert records[0].payload["legacy_event"] == "post_node_execute"


class TestTrajectoryRecorder:
    async def test_attach_registers_all_events(self, tmp_trajectory_dir, tmp_session_store):
        hm = HookManager()
        rec = TrajectoryRecorder(
            hm, session_store=tmp_session_store, session_id="s1", trajectory_dir=tmp_trajectory_dir
        )
        rec.attach()
        registered = set(hm.get_registered_events())
        for evt in HookEvent:
            assert evt.value in registered

    async def test_hook_persists_to_jsonl(self, tmp_trajectory_dir, tmp_session_store):
        hm = HookManager()
        rec = TrajectoryRecorder(
            hm, session_store=tmp_session_store, session_id="s1", trajectory_dir=tmp_trajectory_dir
        )
        rec.attach()
        await hm.fire(
            HookEvent.WORKFLOW_START,
            {
                "execution_id": "exec_1",
                "workflow_id": "wf_1",
                "workflow_name": "demo",
            },
        )
        await hm.fire(
            HookEvent.POST_NODE_EXECUTE,
            {
                "node_id": "n1",
                "node_name": "mock",
                "status": "success",
                "execution_time": 0.5,
                "summary": "ok",
            },
        )
        await hm.fire(
            HookEvent.NODE_ERROR,
            {
                "node_id": "n2",
                "node_name": "bad",
                "error": "boom",
            },
        )
        await hm.fire(
            HookEvent.WORKFLOW_END,
            {
                "execution_id": "exec_1",
                "workflow_id": "wf_1",
                "status": "completed",
            },
        )
        lines = Path(tmp_trajectory_dir, "s1.jsonl").read_text(encoding="utf-8").strip().split("\n")
        raws = [json.loads(ln) for ln in lines]
        legacy = [r["payload"]["legacy_event"] for r in raws]
        assert "workflow_start" in legacy
        assert "post_node_execute" in legacy
        assert "node_error" in legacy
        assert "workflow_end" in legacy
        # normative kernel events carry the semantics
        assert raws[0]["event"] == "start" and raws[0]["run_id"] == "exec_1"
        err_evt = next(r for r in raws if r["payload"]["legacy_event"] == "node_error")
        assert err_evt["event"] == "error"
        assert err_evt["payload"]["is_error"] is True
        assert err_evt["payload"]["error"] == "boom"

    async def test_steps_snapshot_backfilled_on_workflow_end(self, tmp_trajectory_dir, tmp_session_store):
        hm = HookManager()
        session = Session(workflow_id="wf_1", workflow_name="demo")
        tmp_session_store.save(session)
        rec = TrajectoryRecorder(
            hm, session_store=tmp_session_store, session_id=session.id, trajectory_dir=tmp_trajectory_dir
        )
        rec.attach()
        await hm.fire(
            HookEvent.WORKFLOW_START,
            {
                "execution_id": "exec_1",
                "workflow_id": "wf_1",
                "workflow_name": "demo",
            },
        )
        await hm.fire(
            HookEvent.POST_NODE_EXECUTE,
            {
                "node_id": "n1",
                "node_name": "mock",
                "status": "success",
                "execution_time": 0.5,
                "summary": "ok",
            },
        )
        await hm.fire(
            HookEvent.POST_NODE_EXECUTE,
            {
                "node_id": "n2",
                "node_name": "mock2",
                "status": "success",
                "execution_time": 0.3,
                "summary": "ok2",
            },
        )
        await hm.fire(
            HookEvent.WORKFLOW_END,
            {
                "execution_id": "exec_1",
                "workflow_id": "wf_1",
                "status": "completed",
            },
        )
        reloaded = tmp_session_store.get(session.id)
        assert len(reloaded.steps_snapshot) == 2
        assert reloaded.steps_snapshot[0]["node_id"] == "n1"
        assert reloaded.steps_snapshot[1]["node_id"] == "n2"

    async def test_bind_session_switch(self, tmp_trajectory_dir, tmp_session_store):
        hm = HookManager()
        rec = TrajectoryRecorder(hm, session_store=tmp_session_store, trajectory_dir=tmp_trajectory_dir)
        rec.bind_session("s1")
        rec.attach()
        await hm.fire(HookEvent.WORKFLOW_START, {"execution_id": "e1", "workflow_id": "w1"})
        assert Path(tmp_trajectory_dir, "s1.jsonl").exists()

    def test_attach_idempotent(self, tmp_trajectory_dir, tmp_session_store):
        hm = HookManager()
        rec = TrajectoryRecorder(hm, session_store=tmp_session_store, trajectory_dir=tmp_trajectory_dir)
        rec.attach()
        rec.attach()
        assert len(hm.get_registered_events()) == len(list(HookEvent))


class TestSpaceTrajectoryExporter:
    def test_export_message_writes_jsonl(self, tmp_trajectory_dir):
        exporter = SpaceTrajectoryExporter(tmp_trajectory_dir)
        msg = SpaceMessage(id="m1", space_id="sp1", role="user", content="hello")
        path = exporter.export_message(msg)
        assert Path(path).exists()
        data = json.loads(Path(path).read_text(encoding="utf-8").strip())
        assert data["msg_id"] == "m1"
        assert data["space_id"] == "sp1"
        assert data["is_retry"] is False

    def test_export_retry_message_marks_is_retry(self, tmp_trajectory_dir):
        exporter = SpaceTrajectoryExporter(tmp_trajectory_dir)
        msg = SpaceMessage(
            id="m2",
            space_id="sp1",
            role="assistant",
            parent_msg_id="m1",
            thread_id="th_1",
        )
        exporter.export_message(msg)
        path = exporter._path("sp1")
        data = json.loads(path.read_text(encoding="utf-8").strip())
        assert data["is_retry"] is True
        assert data["parent_msg_id"] == "m1"
        assert data["thread_id"] == "th_1"

    def test_list_space_trajectories(self, tmp_trajectory_dir):
        exporter = SpaceTrajectoryExporter(tmp_trajectory_dir)
        exporter.export_message(SpaceMessage(id="m1", space_id="sp1"))
        exporter.export_message(SpaceMessage(id="m2", space_id="sp2"))
        names = exporter.list_space_trajectories()
        assert "space_sp1.jsonl" in names
        assert "space_sp2.jsonl" in names

    async def test_export_space_full_dump(self, tmp_trajectory_dir):
        with tempfile.TemporaryDirectory() as d:
            exporter = SpaceTrajectoryExporter(tmp_trajectory_dir)
            store = SpaceStore(data_dir=d, trajectory_exporter=exporter)
            await store.add_message(SpaceMessage(id="m1", space_id="sp1", content="a"))
            await store.add_message(
                SpaceMessage(
                    id="m2",
                    space_id="sp1",
                    content="b",
                    parent_msg_id="m1",
                    thread_id="th_1",
                )
            )
            count = await exporter.export_space(store, "sp1")
            assert count == 2
            lines = exporter._path("sp1").read_text(encoding="utf-8").strip().split("\n")
            assert len(lines) == 2

    async def test_store_add_message_triggers_export(self, tmp_trajectory_dir):
        with tempfile.TemporaryDirectory() as d:
            exporter = SpaceTrajectoryExporter(tmp_trajectory_dir)
            store = SpaceStore(data_dir=d, trajectory_exporter=exporter)
            await store.add_message(SpaceMessage(id="m1", space_id="spX", content="hi"))
            assert Path(tmp_trajectory_dir, "space_spX.jsonl").exists()
            data = json.loads(Path(tmp_trajectory_dir, "space_spX.jsonl").read_text(encoding="utf-8").strip())
            assert data["msg_id"] == "m1"
