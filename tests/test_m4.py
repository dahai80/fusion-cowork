"""M4 里程碑测试 — DeskRPC事件/会话/权限、CLI权限命令。"""
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
from fusion_desk.engine.node import BaseNode, NodeConfig, NodeResult, NodeStatus
from fusion_desk.engine.workflow import Workflow, WorkflowEngine, WorkflowStatus
from fusion_desk.server.desk_rpc import DeskRPCServer


class _OkNode(BaseNode):
    name = "file_input"
    display_name = "File Input"

    def __init__(self, **kw):
        super().__init__(**kw)
        self.config = NodeConfig()

    async def execute(self, params):
        return NodeResult(status=NodeStatus.SUCCESS, data={"content": "ok"})


# ── DeskRPC 事件 ──

class TestDeskRPCEvents:
    @pytest.mark.asyncio
    async def test_events_subscribe(self):
        em = EventEmitter()
        rpc = DeskRPCServer(event_emitter=em)
        result = await rpc._handle_events_subscribe({})
        assert "sub_id" in result
        assert em.subscriber_count == 1

    @pytest.mark.asyncio
    async def test_events_subscribe_no_emitter(self):
        rpc = DeskRPCServer()
        result = await rpc._handle_events_subscribe({})
        assert "error" in result

    @pytest.mark.asyncio
    async def test_events_recent(self):
        em = EventEmitter()
        rpc = DeskRPCServer(event_emitter=em)
        em.create_event("workflow_start", execution_id="e1")
        result = await rpc._handle_events_recent({})
        assert result["count"] == 1
        assert result["events"][0]["event_type"] == "workflow_start"

    @pytest.mark.asyncio
    async def test_events_recent_with_since(self):
        em = EventEmitter()
        rpc = DeskRPCServer(event_emitter=em)
        em.create_event("log", data={"i": 1})
        time.sleep(0.01)
        cutoff = time.time()
        em.create_event("log", data={"i": 2})
        result = await rpc._handle_events_recent({"since": cutoff})
        assert result["count"] == 1


# ── DeskRPC 会话 ──

class TestDeskRPCSession:
    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = SessionStore(db_path=self.tmp.name)

    def teardown_method(self):
        os.unlink(self.tmp.name)

    @pytest.mark.asyncio
    async def test_session_list(self):
        rpc = DeskRPCServer(session_store=self.store)
        self.store.save(Session(workflow_name="test_wf"))
        result = await rpc._handle_session_list({})
        assert result["count"] == 1

    @pytest.mark.asyncio
    async def test_session_get(self):
        rpc = DeskRPCServer(session_store=self.store)
        s = Session(workflow_name="test_wf")
        self.store.save(s)
        result = await rpc._handle_session_get({"session_id": s.id})
        assert result["workflow_name"] == "test_wf"

    @pytest.mark.asyncio
    async def test_session_fork(self):
        rpc = DeskRPCServer(session_store=self.store)
        s = Session(workflow_name="orig", steps_snapshot=[{"step": 1}])
        self.store.save(s)
        result = await rpc._handle_session_fork({"session_id": s.id, "from_step": 1})
        assert result["status"] == "forked"

    @pytest.mark.asyncio
    async def test_session_no_store(self):
        rpc = DeskRPCServer()
        result = await rpc._handle_session_list({})
        assert "error" in result


# ── DeskRPC 权限 ──

class TestDeskRPCPermission:
    @pytest.mark.asyncio
    async def test_permission_check(self):
        pm = PermissionManager(level=PermissionLevel.BYPASS)
        rpc = DeskRPCServer(permission_manager=pm)
        result = await rpc._handle_permission_check({"tool_name": "shell_exec"})
        assert result["allowed"] is True

    @pytest.mark.asyncio
    async def test_permission_check_denied(self):
        pm = PermissionManager(level=PermissionLevel.MANUAL)
        rpc = DeskRPCServer(permission_manager=pm)
        result = await rpc._handle_permission_check({"tool_name": "shell_exec"})
        assert result["allowed"] is False

    @pytest.mark.asyncio
    async def test_permission_approve(self):
        pm = PermissionManager(level=PermissionLevel.AUTO)
        rpc = DeskRPCServer(permission_manager=pm)
        result = await rpc._handle_permission_approve({"tool_name": "shell_exec", "scope": "command:git *"})
        assert result["approved"] is True

    @pytest.mark.asyncio
    async def test_permission_deny(self):
        pm = PermissionManager(level=PermissionLevel.PLAN)
        rpc = DeskRPCServer(permission_manager=pm)
        result = await rpc._handle_permission_deny({"tool_name": "shell_exec"})
        assert result["denied"] is True

    @pytest.mark.asyncio
    async def test_permission_list(self):
        pm = PermissionManager(level=PermissionLevel.AUTO)
        pm.approve("shell_exec", scope="command:git *")
        rpc = DeskRPCServer(permission_manager=pm)
        result = await rpc._handle_permission_list({})
        assert result["level"] == "auto"
        assert len(result["rules"]) == 1

    @pytest.mark.asyncio
    async def test_permission_no_manager(self):
        rpc = DeskRPCServer()
        result = await rpc._handle_permission_check({"tool_name": "test"})
        assert "error" in result


# ── DeskRPC 构造器 ──

class TestDeskRPCConstructor:
    def test_accepts_all_m2_modules(self):
        pm = PermissionManager()
        hm = HookManager()
        em = EventEmitter()
        store = SessionStore(db_path=tempfile.mktemp(suffix=".db"))
        rpc = DeskRPCServer(
            permission_manager=pm, hook_manager=hm,
            event_emitter=em, session_store=store,
        )
        assert rpc._permission_manager is pm
        assert rpc._hook_manager is hm
        assert rpc._event_emitter is em
        assert rpc._session_store is store

    def test_handler_count(self):
        rpc = DeskRPCServer()
        assert len(rpc._handlers) >= 20


# ── CLI 权限命令 ──

class TestCLIPermission:
    def test_permission_level_command(self):
        from click.testing import CliRunner
        from fusion_desk.cli import cli
        runner = CliRunner()
        result = runner.invoke(cli, ["permission", "level", "auto"])
        assert result.exit_code == 0
        assert "auto" in result.output

    def test_permission_list_command(self):
        from click.testing import CliRunner
        from fusion_desk.cli import cli
        runner = CliRunner()
        result = runner.invoke(cli, ["permission", "list"])
        assert result.exit_code == 0
