"""issue #257: guard 拒绝入待审批队列 + 节点默认看门狗 + 心跳事件。"""

from __future__ import annotations

import asyncio

import pytest

from fusion_cowork.engine.node import BaseNode, NodeResult, NodeStatus, register_node
from fusion_cowork.engine.permission_manager import PermissionManager
from fusion_cowork.engine.workflow import Workflow, WorkflowEngine


def _fresh_pm() -> PermissionManager:
    return PermissionManager()


class TestGuardDenyPending:
    @pytest.mark.asyncio
    async def test_guard_block_registers_pending(self, monkeypatch):
        monkeypatch.setenv("FUSION_GUARD_ENABLED", "1")
        pm = _fresh_pm()

        class FakeVerdict:
            action = "block"
            risk_level = "l4"
            requires_approval = False
            action_id = None
            reason = "policy"

        class FakeClient:
            async def evaluate(self, **kwargs):
                return FakeVerdict()

            async def rules_dump(self):
                return None

        import fusion_cowork.security.guard as guard_mod

        monkeypatch.setattr(guard_mod, "get_guard_client", lambda: FakeClient())
        allowed = await pm.check("shell_exec", "execute", {"command": "rm -rf /"})
        assert allowed is False
        pending = pm.list_pending()
        assert len(pending) == 1
        assert pending[0]["tool_name"] == "shell_exec"
        assert pending[0]["source"] == "local"

    @pytest.mark.asyncio
    async def test_fail_closed_registers_pending(self, monkeypatch, tmp_path):
        monkeypatch.setenv("FUSION_GUARD_ENABLED", "1")
        monkeypatch.setenv("FUSION_GUARD_RULES_CACHE", str(tmp_path / "nope.json"))
        pm = _fresh_pm()

        import fusion_cowork.security.guard as guard_mod

        monkeypatch.setattr(guard_mod, "get_guard_client", lambda: None)
        monkeypatch.setattr(guard_mod, "load_cached_rules", lambda: None)
        allowed = await pm.check("cdp_navigate", "execute", {"url": "https://x"})
        assert allowed is False
        assert len(pm.list_pending()) == 1

    @pytest.mark.asyncio
    async def test_pending_carries_execution_context(self, monkeypatch):
        pm = _fresh_pm()
        from fusion_cowork.engine.permission_manager import set_approval_context

        token = set_approval_context({"workflow": "wf", "node_id": "n1", "execution_id": "run_x"})
        try:
            await pm.check("shell_exec", "execute", {})
        finally:
            import fusion_cowork.engine.permission_manager as m

            m.reset_approval_context(token)
        entry = pm.find_pending_by_execution("run_x")
        assert entry is not None
        assert entry["node_id"] == "n1"


class TestNodeWatchdog:
    def _engine(self, node_timeout_s=None) -> WorkflowEngine:
        return WorkflowEngine(node_timeout_s=node_timeout_s)

    @pytest.mark.asyncio
    async def test_default_timeout_applied(self):
        eng = self._engine()
        assert eng._node_timeout_default == 1800.0

    @pytest.mark.asyncio
    async def test_env_override(self, monkeypatch):
        monkeypatch.setenv("FUSION_COWORK_NODE_TIMEOUT_S", "0")
        eng = self._engine()
        assert eng._node_timeout_default == 0.0

    @pytest.mark.asyncio
    async def test_slow_node_fails_with_reason(self):
        eng = self._engine(node_timeout_s=0.2)

        class SlowNode(BaseNode):
            name = "slow_node"

            async def execute(self, input_data):
                await asyncio.sleep(5)
                return NodeResult(status=NodeStatus.SUCCESS, data={})

        register_node(SlowNode)
        wf = Workflow(name="t")
        wf.add_node(SlowNode("n1"))
        execution = await eng.execute(wf)
        assert execution.status.value == "failed"
        assert "超时" in (execution.error or "")
        assert execution.steps[-1].status == NodeStatus.FAILED

    @pytest.mark.asyncio
    async def test_heartbeat_events_during_long_node(self):
        from fusion_cowork.engine.events import EventEmitter, EventType

        eng = self._engine(node_timeout_s=0)
        eng._node_heartbeat_s = 0.1
        emitter = EventEmitter()
        eng._event_emitter = emitter

        class SlowNode2(BaseNode):
            name = "slow_node2"

            async def execute(self, input_data):
                await asyncio.sleep(1.2)
                return NodeResult(status=NodeStatus.SUCCESS, data={})

        register_node(SlowNode2)
        wf = Workflow(name="t2")
        wf.add_node(SlowNode2("n1"))
        execution = await eng.execute(wf)
        assert execution.status.value == "success"
        events = [e for e in emitter.get_buffered() if e.event_type == EventType.PROGRESS.value]
        assert any(ev.data.get("heartbeat") for ev in events), [e.event_type for e in emitter.get_buffered()]
