"""M2 里程碑测试 — 权限模型、Hook系统、会话持久化、流式事件。"""
import asyncio
import json
import os
import tempfile
import time

import pytest

from fusion_cowork.engine.permission import (
    PermissionManager, PermissionLevel, Permission, HIGH_RISK_NODES,
)
from fusion_cowork.engine.hooks import HookManager, HookEvent, HookContext
from fusion_cowork.engine.session import Session, SessionStore
from fusion_cowork.engine.events import EventType, WorkflowEvent, EventEmitter
from fusion_cowork.engine.node import (
    BaseNode, NodeConfig, NodeResult, NodeStatus, NodeRegistry, register_node,
)
from fusion_cowork.engine.workflow import (
    Workflow, WorkflowEngine, WorkflowExecution, WorkflowStatus, Edge, WorkflowStep,
)


# ── 权限模型 ──

class TestPermissionLevel:
    def test_levels_exist(self):
        assert PermissionLevel.MANUAL.value == "manual"
        assert PermissionLevel.AUTO.value == "auto"
        assert PermissionLevel.PLAN.value == "plan"
        assert PermissionLevel.BYPASS.value == "bypass"

    def test_high_risk_nodes(self):
        assert "shell_exec" in HIGH_RISK_NODES
        assert "python_repl" in HIGH_RISK_NODES
        assert "file_input" not in HIGH_RISK_NODES


class TestPermission:
    def test_matches_exact(self):
        p = Permission(tool_name="shell_exec", allowed=False, scope="*")
        assert p.matches("shell_exec", {"command": "rm -rf"})

    def test_matches_scope_pattern(self):
        p = Permission(tool_name="*", allowed=True, scope="file:~/Desktop/**")
        assert p.matches("file_input", {"path": "~/Desktop/test.txt"})


class TestPermissionManager:
    @pytest.mark.asyncio
    async def test_bypass_allows_all(self):
        pm = PermissionManager(level=PermissionLevel.BYPASS)
        assert await pm.check("shell_exec") is True
        assert await pm.check("anything") is True

    @pytest.mark.asyncio
    async def test_manual_blocks_high_risk(self):
        pm = PermissionManager(level=PermissionLevel.MANUAL)
        assert await pm.check("shell_exec") is False
        assert await pm.check("python_repl") is False

    @pytest.mark.asyncio
    async def test_manual_allows_safe(self):
        pm = PermissionManager(level=PermissionLevel.MANUAL)
        assert await pm.check("file_input") is False

    @pytest.mark.asyncio
    async def test_auto_blocks_high_risk(self):
        pm = PermissionManager(level=PermissionLevel.AUTO)
        assert await pm.check("shell_exec") is False

    @pytest.mark.asyncio
    async def test_auto_allows_approved(self):
        pm = PermissionManager(level=PermissionLevel.AUTO)
        pm.approve("shell_exec", scope="command:git *")
        assert await pm.check("shell_exec", params={"command": "git status"}) is True

    @pytest.mark.asyncio
    async def test_deny_overrides(self):
        pm = PermissionManager(level=PermissionLevel.PLAN)
        pm.deny("shell_exec")
        assert await pm.check("shell_exec") is False

    @pytest.mark.asyncio
    async def test_save_load(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            pm = PermissionManager(level=PermissionLevel.AUTO)
            pm.approve("shell_exec", scope="command:git *")
            pm.save(path=path)
            pm2 = PermissionManager(level=PermissionLevel.AUTO)
            pm2.load(path=path)
            assert await pm2.check("shell_exec", params={"command": "git status"}) is True
        finally:
            os.unlink(path)


# ── Hook 系统 ──

class TestHookEvent:
    def test_all_events(self):
        expected = [
            "PRE_NODE_EXECUTE", "POST_NODE_EXECUTE",
            "WORKFLOW_START", "WORKFLOW_END", "WORKFLOW_CANCEL",
            "PERMISSION_REQUEST", "CONFIG_CHANGE",
            "AGENT_START", "AGENT_STOP",
            "NOTIFICATION", "NODE_ERROR",
        ]
        for name in expected:
            assert hasattr(HookEvent, name)


class TestHookContext:
    def test_cancel(self):
        ctx = HookContext(event=HookEvent.PRE_NODE_EXECUTE, data={"x": 1})
        assert not ctx.cancelled
        ctx.cancel()
        assert ctx.cancelled

    def test_modify(self):
        ctx = HookContext(event=HookEvent.PRE_NODE_EXECUTE, data={"x": 1})
        ctx.modify("x", 2)
        assert ctx.modified_data == {"x": 2}


class TestHookManager:
    @pytest.mark.asyncio
    async def test_register_and_fire(self):
        hm = HookManager()
        received = []
        async def handler(ctx):
            received.append(ctx.data)
            return ctx
        hm.register(HookEvent.WORKFLOW_START, handler)
        await hm.fire(HookEvent.WORKFLOW_START, {"name": "test"})
        assert len(received) == 1
        assert received[0]["name"] == "test"

    @pytest.mark.asyncio
    async def test_cancel_in_handler(self):
        hm = HookManager()
        async def handler(ctx):
            ctx.cancel()
            return ctx
        hm.register(HookEvent.PRE_NODE_EXECUTE, handler)
        ctx = await hm.fire(HookEvent.PRE_NODE_EXECUTE, {})
        assert ctx.cancelled

    @pytest.mark.asyncio
    async def test_no_handler_returns_ctx(self):
        hm = HookManager()
        ctx = await hm.fire(HookEvent.POST_NODE_EXECUTE, {"data": 42})
        assert not ctx.cancelled
        assert ctx.data == {"data": 42}

    @pytest.mark.asyncio
    async def test_unregister(self):
        hm = HookManager()
        called = []
        async def handler(ctx):
            called.append(1)
            return ctx
        hm.register(HookEvent.WORKFLOW_END, handler)
        hm.unregister(HookEvent.WORKFLOW_END, handler)
        await hm.fire(HookEvent.WORKFLOW_END, {})
        assert len(called) == 0


# ── 会话持久化 ──

class TestSession:
    def test_auto_id(self):
        s = Session()
        assert s.id.startswith("sess_")
        assert s.created_at > 0

    def test_custom_id(self):
        s = Session(id="my_session")
        assert s.id == "my_session"


class TestSessionStore:
    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = SessionStore(db_path=self.tmp.name)

    def teardown_method(self):
        os.unlink(self.tmp.name)

    def test_save_and_get(self):
        s = Session(workflow_id="wf_1", workflow_name="test_wf")
        self.store.save(s)
        got = self.store.get(s.id)
        assert got is not None
        assert got.workflow_name == "test_wf"

    def test_list_sessions(self):
        for i in range(5):
            self.store.save(Session(workflow_name=f"wf_{i}"))
        sessions = self.store.list_sessions(limit=3)
        assert len(sessions) == 3

    def test_update_status(self):
        s = Session()
        self.store.save(s)
        self.store.update_status(s.id, "running")
        got = self.store.get(s.id)
        assert got.status == "running"

    def test_fork(self):
        s = Session(workflow_name="original", steps_snapshot=[{"step": 1}, {"step": 2}])
        self.store.save(s)
        forked = self.store.fork(s.id, from_step=1)
        assert forked is not None
        assert forked.status == "forked"
        assert len(forked.steps_snapshot) == 1
        assert forked.metadata["forked_from"] == s.id

    def test_delete(self):
        s = Session()
        self.store.save(s)
        assert self.store.delete(s.id) is True
        assert self.store.get(s.id) is None

    def test_cleanup_expired(self):
        s = Session(status="completed")
        self.store.save(s)
        # manually set updated_at to expired
        with self.store._connect() as conn:
            conn.execute(
                "UPDATE sessions SET updated_at=? WHERE id=?",
                (time.time() - 100 * 86400, s.id),
            )
        count = self.store.cleanup_expired(expire_days=30)
        assert count == 1

    def test_to_dict(self):
        s = Session(workflow_name="test")
        d = self.store.to_dict(s)
        assert d["workflow_name"] == "test"
        assert "id" in d


# ── 流式事件 ──

class TestWorkflowEvent:
    def test_auto_fields(self):
        e = WorkflowEvent(event_type="node_start")
        assert e.event_id.startswith("evt_")
        assert e.timestamp > 0

    def test_to_sse(self):
        e = WorkflowEvent(event_type="node_start", execution_id="exec_1")
        sse = e.to_sse()
        assert "event: node_start" in sse
        assert "exec_1" in sse


class TestEventEmitter:
    @pytest.mark.asyncio
    async def test_subscribe_and_receive(self):
        em = EventEmitter()
        sub_id, queue = em.subscribe()
        em.create_event("workflow_start", execution_id="e1")
        event = await asyncio.wait_for(queue.get(), timeout=1.0)
        assert event.event_type == "workflow_start"
        em.unsubscribe(sub_id)

    @pytest.mark.asyncio
    async def test_buffer(self):
        em = EventEmitter(buffer_size=5)
        for i in range(10):
            em.create_event("log", data={"i": i})
        buffered = em.get_buffered()
        assert len(buffered) == 5
        assert buffered[0].data["i"] == 5

    @pytest.mark.asyncio
    async def test_callback(self):
        em = EventEmitter()
        received = []
        em.on_event(lambda e: received.append(e))
        em.create_event("progress", data={"pct": 50})
        assert len(received) == 1

    @pytest.mark.asyncio
    async def test_subscriber_count(self):
        em = EventEmitter()
        assert em.subscriber_count == 0
        _, q1 = em.subscribe("s1")
        assert em.subscriber_count == 1
        em.unsubscribe("s1")
        assert em.subscriber_count == 0


# ── WorkflowEngine 集成 ──

class _OkNode(BaseNode):
    name = "ok_node"
    display_name = "OK Node"

    def __init__(self, **kw):
        super().__init__(**kw)
        self.config = NodeConfig()

    async def execute(self, params):
        return NodeResult(status=NodeStatus.SUCCESS, data={"result": "ok"})


class TestWorkflowEngineM2:
    @pytest.mark.asyncio
    async def test_hooks_fire_on_execute(self):
        hm = HookManager()
        pm = PermissionManager(level=PermissionLevel.BYPASS)
        engine = WorkflowEngine(permission_manager=pm, hook_manager=hm)

        events = []
        async def capture(ctx):
            events.append(ctx.event.value)
            return ctx
        for evt in [HookEvent.WORKFLOW_START, HookEvent.WORKFLOW_END,
                     HookEvent.PRE_NODE_EXECUTE, HookEvent.POST_NODE_EXECUTE]:
            hm.register(evt, capture)

        wf = Workflow(name="hook_test", workflow_id="wf_m2")
        node = _OkNode()
        wf.add_node(node)

        await engine.execute(wf)
        assert "workflow_start" in events
        assert "workflow_end" in events
        assert "pre_node_execute" in events
        assert "post_node_execute" in events

    @pytest.mark.asyncio
    async def test_permission_denies_high_risk(self):
        hm = HookManager()
        pm = PermissionManager(level=PermissionLevel.MANUAL)
        engine = WorkflowEngine(permission_manager=pm, hook_manager=hm)

        class _ShellNode(BaseNode):
            name = "shell_exec"
            display_name = "Shell"

            def __init__(self, **kw):
                super().__init__(**kw)
                self.config = NodeConfig()

            async def execute(self, params):
                return NodeResult(status=NodeStatus.SUCCESS, data={"out": "done"})

        wf = Workflow(name="deny_test", workflow_id="wf_deny")
        wf.add_node(_ShellNode())
        result = await engine.execute(wf)
        assert result.status == WorkflowStatus.FAILED
        assert any(s.status == NodeStatus.DENIED for s in result.steps)

    @pytest.mark.asyncio
    async def test_hook_cancels_node(self):
        hm = HookManager()
        pm = PermissionManager(level=PermissionLevel.BYPASS)
        engine = WorkflowEngine(permission_manager=pm, hook_manager=hm)

        async def cancel_handler(ctx):
            ctx.cancel()
            return ctx
        hm.register(HookEvent.PRE_NODE_EXECUTE, cancel_handler)

        wf = Workflow(name="cancel_test", workflow_id="wf_cancel")
        wf.add_node(_OkNode())
        result = await engine.execute(wf)
        assert any(s.status == NodeStatus.CANCELLED for s in result.steps)

    @pytest.mark.asyncio
    async def test_no_hook_no_permission(self):
        engine = WorkflowEngine()
        wf = Workflow(name="plain_test", workflow_id="wf_plain")
        wf.add_node(_OkNode())
        result = await engine.execute(wf)
        assert result.status == WorkflowStatus.SUCCESS
