"""M3 里程碑测试 — MCP权限拦截、SSE事件流、Session+WorkflowEngine集成。"""
import asyncio
import json
import os
import tempfile
import time

import pytest

from fusion_desk.engine.permission import PermissionManager, PermissionLevel
from fusion_desk.engine.hooks import HookManager, HookEvent
from fusion_desk.engine.session import Session, SessionStore
from fusion_desk.engine.events import EventType, WorkflowEvent, EventEmitter
from fusion_desk.engine.node import BaseNode, NodeConfig, NodeResult, NodeStatus, NodeRegistry
from fusion_desk.engine.workflow import Workflow, WorkflowEngine, WorkflowStatus
from fusion_desk.server.mcp_server import MCPToolRegistry, MCPServer


class _OkNode(BaseNode):
    name = "file_input"
    display_name = "File Input"

    def __init__(self, **kw):
        super().__init__(**kw)
        self.config = NodeConfig()

    async def execute(self, params):
        return NodeResult(status=NodeStatus.SUCCESS, data={"content": "ok"})


class _ShellNode(BaseNode):
    name = "shell_exec"
    display_name = "Shell"

    def __init__(self, **kw):
        super().__init__(**kw)
        self.config = NodeConfig()

    async def execute(self, params):
        return NodeResult(status=NodeStatus.SUCCESS, data={"stdout": "done"})


# ── MCP Tool 权限拦截 ──

class TestMCPPermission:
    @pytest.mark.asyncio
    async def test_mcp_blocks_high_risk_manual(self):
        pm = PermissionManager(level=PermissionLevel.MANUAL)
        hm = HookManager()
        registry = MCPToolRegistry(permission_manager=pm, hook_manager=hm)
        registry.register_tools()
        result = await registry.call_tool("run_terminal", {"command": "ls"})
        body = result["content"][0]["text"]
        assert "denied" in body.lower() or "权限" in body

    @pytest.mark.asyncio
    async def test_mcp_allows_safe_auto(self):
        pm = PermissionManager(level=PermissionLevel.AUTO)
        registry = MCPToolRegistry(permission_manager=pm)
        registry.register_tools()
        result = await registry.call_tool("read_file", {"path": "/tmp/test_m3.txt"})
        body = json.loads(result["content"][0]["text"])
        assert body.get("status") != "denied"

    @pytest.mark.asyncio
    async def test_mcp_hook_cancel(self):
        pm = PermissionManager(level=PermissionLevel.BYPASS)
        hm = HookManager()

        async def cancel_handler(ctx):
            ctx.cancel()
            return ctx
        hm.register(HookEvent.PRE_NODE_EXECUTE, cancel_handler)

        registry = MCPToolRegistry(permission_manager=pm, hook_manager=hm)
        registry.register_tools()
        result = await registry.call_tool("read_file", {"path": "/tmp/test.txt"})
        body = json.loads(result["content"][0]["text"])
        assert body.get("status") == "cancelled"

    @pytest.mark.asyncio
    async def test_mcp_hook_fires_permission_request(self):
        pm = PermissionManager(level=PermissionLevel.MANUAL)
        hm = HookManager()
        events = []

        async def capture(ctx):
            events.append(ctx.event)
            return ctx
        hm.register(HookEvent.PERMISSION_REQUEST, capture)

        registry = MCPToolRegistry(permission_manager=pm, hook_manager=hm)
        registry.register_tools()
        await registry.call_tool("run_terminal", {"command": "ls"})
        assert any(e == HookEvent.PERMISSION_REQUEST for e in events)

    @pytest.mark.asyncio
    async def test_mcp_hook_modify_params(self):
        pm = PermissionManager(level=PermissionLevel.BYPASS)
        hm = HookManager()

        async def modify_handler(ctx):
            if ctx.data.get("node_name") == "file_input":
                ctx.modify("input_data", {"path": "/tmp/modified.txt"})
            return ctx
        hm.register(HookEvent.PRE_NODE_EXECUTE, modify_handler)

        registry = MCPToolRegistry(permission_manager=pm, hook_manager=hm)
        registry.register_tools()
        result = await registry.call_tool("read_file", {"path": "/tmp/original.txt"})
        assert "isError" not in result or not result.get("isError")


# ── EventEmitter + SSE ──

class TestEventEmitterSSE:
    @pytest.mark.asyncio
    async def test_subscribe_receives_events(self):
        em = EventEmitter()
        sub_id, queue = em.subscribe()
        em.create_event(EventType.WORKFLOW_START, execution_id="e1")
        em.create_event(EventType.NODE_START, execution_id="e1", node_name="test")
        evt1 = await asyncio.wait_for(queue.get(), timeout=1.0)
        evt2 = await asyncio.wait_for(queue.get(), timeout=1.0)
        assert evt1.event_type == "workflow_start"
        assert evt2.event_type == "node_start"
        em.unsubscribe(sub_id)

    @pytest.mark.asyncio
    async def test_sse_format(self):
        evt = WorkflowEvent(event_type="node_end", execution_id="e1", node_name="shell_exec")
        sse = evt.to_sse()
        assert "event: node_end" in sse
        assert "shell_exec" in sse

    @pytest.mark.asyncio
    async def test_buffer_replay(self):
        em = EventEmitter(buffer_size=10)
        for i in range(5):
            em.create_event("log", data={"i": i})
        buffered = em.get_buffered(since=time.time() - 60)
        assert len(buffered) == 5


# ── WorkflowEngine + Session + Event 集成 ──

class TestWorkflowEngineSessionEvent:
    @pytest.mark.asyncio
    async def test_auto_session_creation(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            store = SessionStore(db_path=db_path)
            em = EventEmitter()
            engine = WorkflowEngine(session_store=store, event_emitter=em)
            wf = Workflow(name="session_test", workflow_id="wf_s1")
            wf.add_node(_OkNode())
            result = await engine.execute(wf)
            assert result.status == WorkflowStatus.SUCCESS

            sessions = store.list_sessions(limit=1)
            assert len(sessions) == 1
            assert sessions[0].workflow_name == "session_test"
            assert sessions[0].status == "success"
        finally:
            os.unlink(db_path)

    @pytest.mark.asyncio
    async def test_event_emission_during_execution(self):
        em = EventEmitter()
        engine = WorkflowEngine(event_emitter=em)
        wf = Workflow(name="event_test", workflow_id="wf_ev1")
        wf.add_node(_OkNode())

        sub_id, queue = em.subscribe()
        await engine.execute(wf)

        events = []
        while not queue.empty():
            events.append(await queue.get())
        em.unsubscribe(sub_id)

        event_types = [e.event_type for e in events]
        assert "workflow_start" in event_types
        assert "node_start" in event_types
        assert "node_end" in event_types
        assert "workflow_end" in event_types

    @pytest.mark.asyncio
    async def test_session_steps_updated(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            store = SessionStore(db_path=db_path)
            engine = WorkflowEngine(session_store=store)
            wf = Workflow(name="steps_test", workflow_id="wf_st1")
            wf.add_node(_OkNode())
            await engine.execute(wf)

            sessions = store.list_sessions(limit=1)
            assert len(sessions) == 1
            assert len(sessions[0].steps_snapshot) >= 1
        finally:
            os.unlink(db_path)

    @pytest.mark.asyncio
    async def test_full_stack_integration(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            pm = PermissionManager(level=PermissionLevel.BYPASS)
            hm = HookManager()
            store = SessionStore(db_path=db_path)
            em = EventEmitter()
            engine = WorkflowEngine(
                permission_manager=pm, hook_manager=hm,
                session_store=store, event_emitter=em,
            )

            hook_events = []
            async def capture_hook(ctx):
                hook_events.append(ctx.event.value)
                return ctx
            hm.register(HookEvent.WORKFLOW_START, capture_hook)
            hm.register(HookEvent.WORKFLOW_END, capture_hook)

            sub_id, queue = em.subscribe()

            wf = Workflow(name="full_stack", workflow_id="wf_fs1")
            wf.add_node(_OkNode())
            result = await engine.execute(wf)

            assert result.status == WorkflowStatus.SUCCESS
            assert "workflow_start" in hook_events
            assert "workflow_end" in hook_events

            sse_events = []
            while not queue.empty():
                sse_events.append(await queue.get())
            em.unsubscribe(sub_id)
            assert len(sse_events) >= 3

            sessions = store.list_sessions(limit=1)
            assert sessions[0].status == "success"
        finally:
            os.unlink(db_path)


# ── MCPServer 集成 ──

class TestMCPServerM3:
    def test_server_accepts_permission_hook(self):
        pm = PermissionManager(level=PermissionLevel.AUTO)
        hm = HookManager()
        server = MCPServer(permission_manager=pm, hook_manager=hm)
        assert server._registry._permission_manager is pm
        assert server._registry._hook_manager is hm

    def test_server_default_no_permission(self):
        server = MCPServer()
        assert server._registry._permission_manager is None
        assert server._registry._hook_manager is None
