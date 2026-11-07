"""issue #372: crash 证据日志 + 僵尸 resume 真实重启 + 停滞看门狗。

- recover_persisted crash 恢复日志必须点名崩溃时执行中的节点 (证据, 不再静默)
- resume() 在引擎无暂停事件 (执行任务已死) 时从快照真实重启, 不再假回 running
- 停滞看门狗: running 无事件超阈值 → crash-pause + 证据 + 取消死任务
"""

import asyncio
import json
import logging

import pytest

from fusion_cowork.engine.node import BaseNode, NodeRegistry, NodeResult, NodeStatus, register_node
from fusion_cowork.server.web_api import RunRegistry as RunsAPI


class MockOk372(BaseNode):
    name = "mock_ok_372"
    display_name = "Mock OK 372"
    category = "tool"
    description = "mock"
    icon = "x"
    default_label = "mock"

    async def execute(self, inputs):
        return NodeResult(status=NodeStatus.SUCCESS, data={"ok": True}, summary="ok")


@pytest.fixture()
def _mock_node():
    # 测试期注册/结束注销 — 模块级注册会污染依赖节点清单的其他测试
    register_node(MockOk372)
    yield
    NodeRegistry.unregister("mock_ok_372", force=True)


def _wf_def():
    return {"name": "t372", "nodes": [{"id": "n1", "name": "mock_ok_372", "params": {}}], "edges": []}


class _FakeExecution:
    def __init__(self):
        self.steps = []
        self.status = NodeStatus.SUCCESS
        self.result_summary = ""
        self.error = None


class _FakeEngine:
    _node_heartbeat_s = 30.0

    def __init__(self, resume_result=False):
        self._resume_result = resume_result
        self.executed = []
        self.cancelled = []

    def resume(self, run_id):
        return self._resume_result

    def pause(self, run_id):
        return True

    def cancel(self, run_id):
        self.cancelled.append(run_id)

    def hitl_pending(self, run_id):
        return False

    def get_execution(self, run_id):
        raise KeyError(run_id)

    async def execute(self, wf, initial_input=None, execution_id="", **kwargs):
        self.executed.append({"wf": wf, "kwargs": kwargs})
        return _FakeExecution()


def _record(engine, **overrides):
    api = RunsAPI(engine)
    rec = {
        "run_id": "run_372",
        "workflow_id": "wf372",
        "workflow_name": "t372",
        "status": "paused",
        "created_at": 1.0,
        "finished_at": None,
        "steps": [{"node_id": "n1", "node_name": "mock_ok_372", "status": "success"}],
        "result": None,
        "error": None,
        "review": None,
        "workflow_def": _wf_def(),
        "artifacts": [],
        "memory": False,
        "paused_reason": "crash",
        "source": "",
        "labels": {},
        "space_id": "",
    }
    rec.update(overrides)
    api._runs["run_372"] = rec
    return api, rec


@pytest.mark.usefixtures("_mock_node")
class TestResumeRelaunch:
    async def test_dead_engine_resume_relaunches_from_snapshot(self):
        engine = _FakeEngine(resume_result=False)
        api, rec = _record(engine)
        assert api.resume("run_372") is True
        assert rec["status"] == "running"
        assert rec["paused_reason"] == ""
        await asyncio.sleep(0)
        assert len(engine.executed) == 1
        assert engine.executed[0]["kwargs"].get("resume_steps") == [
            {"node_id": "n1", "node_name": "mock_ok_372", "status": "success"}
        ]

    async def test_relaunch_failure_stays_paused_loud(self):
        engine = _FakeEngine(resume_result=False)
        api, rec = _record(engine, workflow_def=None)
        assert api.resume("run_372") is False
        assert rec["status"] == "paused"
        assert rec["paused_reason"] == "crash"
        assert "resume" in rec["error"]

    async def test_live_engine_resume_keeps_engine_path(self):
        engine = _FakeEngine(resume_result=True)
        api, rec = _record(engine)
        assert api.resume("run_372") is True
        assert rec["status"] == "running"
        await asyncio.sleep(0)
        assert engine.executed == []


@pytest.mark.usefixtures("_mock_node")
class TestRecoverCrashEvidence:
    async def test_crash_recovery_logs_running_node(self, caplog):
        engine = _FakeEngine()
        api = RunsAPI(engine)

        class _Store:
            def load_active(self):
                return [
                    {
                        "run_id": "run_372",
                        "workflow_def": json.dumps(_wf_def()),
                        "steps": '[{"node_id": "n7", "node_name": "claude_dispatch", "status": "running"}]',
                        "directives": "[]",
                        "status": "running",
                        "workflow_id": "wf",
                        "workflow_name": "t",
                        "created_at": 1.0,
                        "memory": 1,
                        "space_id": "",
                    }
                ]

            def save(self, record, paused_reason=""):
                pass

            def delete(self, run_id):
                pass

        api.run_store = _Store()
        with caplog.at_level(logging.WARNING):
            n = await api.recover_persisted()
        assert n == 1
        assert "run_372" in caplog.text
        assert "n7" in caplog.text


class TestStallWatchdog:
    async def test_stall_trip_pauses_with_evidence_and_cancels(self):
        engine = _FakeEngine()
        api, rec = _record(engine, status="running", paused_reason="")
        task = asyncio.create_task(asyncio.sleep(60))
        api._tasks["run_372"] = task
        api._stall_trip("run_372", rec, 400.0)
        assert rec["status"] == "paused"
        assert rec["paused_reason"] == "crash"
        assert "400" in rec["error"]
        assert task.cancelling() >= 1

    def test_stall_pass_trips_only_stale_running(self):
        engine = _FakeEngine()
        api, rec = _record(engine, status="running", paused_reason="")
        rec["created_at"] = 1.0
        now = 10000.0
        assert api._stall_pass(now) == 1
        assert rec["status"] == "paused"

    def test_stall_pass_skips_fresh_and_hitl(self):
        engine = _FakeEngine()
        api, rec = _record(engine, status="running", paused_reason="")
        rec["created_at"] = 1.0
        assert api._stall_pass(2.0) == 0
        assert rec["status"] == "running"
        engine.hitl_pending = lambda run_id: True
        assert api._stall_pass(10000.0) == 0
        assert rec["status"] == "running"

    def test_stall_pass_skips_non_running(self):
        engine = _FakeEngine()
        api, rec = _record(engine, status="paused", paused_reason="manual")
        rec["created_at"] = 1.0
        assert api._stall_pass(10000.0) == 0
        assert rec["status"] == "paused"

    def test_sweeper_not_started_without_emitter(self):
        engine = _FakeEngine()
        api, _ = _record(engine)
        api._ensure_stall_sweeper()
        assert api._sweeper_started is False
