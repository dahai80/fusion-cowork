"""issue #378: dual-layer timeout divergence — the engine watchdog at 1800s kills the dispatch node first (child 3600s).

- BaseNode.watchdog_floor defaults to 0; a node may raise the engine watchdog via a self-resolved deadline
- ClaudeDispatchNode.watchdog_floor resolves on the same chain as execute (param > env > 3600)
- node timeout detach result carries explicit outcome=detached_session_still_running + pid
- watchdog cancel log carries node_deadline (both timeouts in one frame)
"""

import asyncio
import logging

import pytest

from fusion_cowork.engine.node import BaseNode, NodeRegistry, NodeResult, NodeStatus, register_node
from fusion_cowork.engine.workflow import WorkflowEngine, WorkflowStatus
from fusion_cowork.nodes.ops.ops_nodes import ClaudeDispatchNode


class MockSlow378(BaseNode):
    name = "mock_slow_378"
    display_name = "Mock Slow 378"
    category = "tool"
    description = "mock"
    icon = "x"
    default_label = "mock"

    async def execute(self, inputs):
        await asyncio.sleep(1.5)
        return NodeResult(status=NodeStatus.SUCCESS, data={"ok": True}, summary="ok")


class MockFloorSlow378(MockSlow378):
    name = "mock_floor_slow_378"

    def watchdog_floor(self) -> float:
        return 50.0


class MockBrokenFloor378(MockSlow378):
    name = "mock_broken_floor_378"

    def watchdog_floor(self):
        raise RuntimeError("boom")


@pytest.fixture()
def _mock_nodes():
    # Register for the test / unregister at the end — module-level registration would pollute other tests that depend on the node list
    for cls in (MockSlow378, MockFloorSlow378, MockBrokenFloor378):
        register_node(cls)
    yield
    for cls in (MockSlow378, MockFloorSlow378, MockBrokenFloor378):
        NodeRegistry.unregister(cls.name, force=True)


def _engine_and_run(node_name, node_timeout_s=1.0):
    wf = {
        "name": "t378",
        "nodes": [{"id": "n1", "name": node_name, "params": {}}],
        "edges": [],
    }
    from fusion_cowork.engine.workflow import Workflow

    engine = WorkflowEngine(node_timeout_s=node_timeout_s)
    return engine, Workflow.from_dict(wf)


@pytest.mark.usefixtures("_mock_nodes")
class TestEngineWatchdogFloor:
    async def test_floor_node_survives_engine_default(self):
        # floor 50 > engine 1.0 -> the 1.5s slow node completes normally, not killed by the watchdog
        engine, wf = _engine_and_run("mock_floor_slow_378", node_timeout_s=1.0)
        exec_res = await engine.execute(wf)
        assert exec_res.status == WorkflowStatus.SUCCESS
        assert exec_res.steps[0].status == NodeStatus.SUCCESS

    async def test_no_floor_node_still_killed_by_watchdog(self):
        engine, wf = _engine_and_run("mock_slow_378", node_timeout_s=1.0)
        exec_res = await engine.execute(wf)
        assert exec_res.status == WorkflowStatus.FAILED
        assert "超时" in (exec_res.steps[0].error or "")  # i18n: user-facing

    async def test_broken_floor_degrades_to_engine_default(self):
        engine, wf = _engine_and_run("mock_broken_floor_378", node_timeout_s=1.0)
        exec_res = await engine.execute(wf)
        assert exec_res.status == WorkflowStatus.FAILED
        assert "超时" in (exec_res.steps[0].error or "")  # i18n: user-facing


class TestDispatchWatchdogFloorResolution:
    def _node(self, **extra):
        node = ClaudeDispatchNode()
        node.config.params = {
            "repo": "o/r",
            "allowed_repos": ["o/r"],
            "issue_number": 1,
            "prompt": "p",
            "cwd": "/tmp",
            **extra,
        }
        return node

    def test_param_wins(self, monkeypatch):
        monkeypatch.setenv("FUSION_COWORK_CLAUDE_DISPATCH_TIMEOUT", "999")
        assert self._node(timeout=5400).watchdog_floor() == 5400.0

    def test_env_fallback(self, monkeypatch):
        monkeypatch.delenv("FUSION_COWORK_CLAUDE_DISPATCH_TIMEOUT", raising=False)
        monkeypatch.setenv("FUSION_COWORK_CLAUDE_DISPATCH_TIMEOUT", "4200")
        assert self._node().watchdog_floor() == 4200.0

    def test_default_3600(self, monkeypatch):
        monkeypatch.delenv("FUSION_COWORK_CLAUDE_DISPATCH_TIMEOUT", raising=False)
        assert self._node().watchdog_floor() == 3600.0

    def test_garbage_param_degrades_to_zero(self, monkeypatch):
        monkeypatch.delenv("FUSION_COWORK_CLAUDE_DISPATCH_TIMEOUT", raising=False)
        assert self._node(timeout="abc").watchdog_floor() == 0.0


class _FakeProc:
    def __init__(self):
        self.pid = 4321
        self.returncode = None

    def communicate(self):
        return asyncio.sleep(30, result=(b"{}", None))


def _dispatch_node(cwd, **extra):
    node = ClaudeDispatchNode()
    node.config.params = {
        "repo": "o/r",
        "allowed_repos": ["o/r"],
        "issue_number": 7,
        "prompt": "work",
        "cwd": str(cwd),
        "timeout": 0.1,
        **extra,
    }
    return node


@pytest.fixture()
def _fake_spawn(monkeypatch):
    started = []

    async def fake_exec_async(*argv, **kwargs):
        proc = _FakeProc()
        started.append(proc)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec_async)
    return started


class TestDispatchTimeoutOutcome:
    async def test_timeout_detach_has_explicit_outcome(self, tmp_path, _fake_spawn):
        res = await _dispatch_node(tmp_path, timeout=0.05).execute({})
        assert res.status.value == "failed"
        assert res.data["outcome"] == "detached_session_still_running"
        assert res.data["detached"] is True
        assert res.data["pid"] == 4321
        assert "超时" in res.error  # i18n: user-facing
        # Teardown: stop the background comm task + watcher, leave no dangling tasks
        for t in asyncio.all_tasks():
            if t is not asyncio.current_task():
                t.cancel()
        await asyncio.gather(
            *[t for t in asyncio.all_tasks() if t is not asyncio.current_task()], return_exceptions=True
        )

    async def test_watchdog_cancel_logs_node_deadline(self, tmp_path, _fake_spawn, caplog):
        node = _dispatch_node(tmp_path, timeout=3600)
        task = asyncio.create_task(node.execute({}))
        await asyncio.sleep(0.05)
        task.cancel()
        with caplog.at_level(logging.WARNING), pytest.raises(asyncio.CancelledError):
            await task
        assert "被看门狗取消" in caplog.text  # i18n: user-facing
        assert "node_deadline=3600s" in caplog.text
        assert "4321" in caplog.text
        for t in asyncio.all_tasks():
            if t is not asyncio.current_task():
                t.cancel()
        await asyncio.gather(
            *[t for t in asyncio.all_tasks() if t is not asyncio.current_task()], return_exceptions=True
        )
