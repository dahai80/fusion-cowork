"""M4 里程碑测试 — DeskRPC事件/会话/权限 + Computer Use + 远程控制 + 结构化输出。"""
import asyncio
import json
import os
import tempfile
import time

import pytest
from unittest.mock import MagicMock, AsyncMock, patch

from fusion_desk.engine.permission import PermissionManager, PermissionLevel
from fusion_desk.engine.hooks import HookManager, HookEvent
from fusion_desk.engine.session import Session, SessionStore
from fusion_desk.engine.events import EventType, WorkflowEvent, EventEmitter
from fusion_desk.engine.node import BaseNode, NodeConfig, NodeResult, NodeStatus, NodeRegistry
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


# ── M4: Computer Use 输入节点 ──

from fusion_desk.nodes.macos.input_nodes import (
    MouseMoveNode, MouseClickNode, KeyboardTypeNode,
    KeyboardShortcutNode, ComputerUseLoopNode,
    _KEY_MAP, _MOD_MAP,
)
from fusion_desk.engine.schema import OutputSchema


class TestInputNodeRegistration:
    def test_mouse_move_registered(self):
        assert NodeRegistry.get("mouse_move") is MouseMoveNode

    def test_mouse_click_registered(self):
        assert NodeRegistry.get("mouse_click") is MouseClickNode

    def test_keyboard_type_registered(self):
        assert NodeRegistry.get("keyboard_type") is KeyboardTypeNode

    def test_keyboard_shortcut_registered(self):
        assert NodeRegistry.get("keyboard_shortcut") is KeyboardShortcutNode

    def test_computer_use_loop_registered(self):
        assert NodeRegistry.get("computer_use_loop") is ComputerUseLoopNode


class TestInputNodeSchemas:
    def test_mouse_move_schema(self):
        schema = MouseMoveNode().get_params_schema()
        assert "x" in schema["properties"]
        assert "y" in schema["required"]

    def test_mouse_click_schema(self):
        schema = MouseClickNode().get_params_schema()
        assert "button" in schema["properties"]

    def test_keyboard_type_schema(self):
        schema = KeyboardTypeNode().get_params_schema()
        assert "text" in schema["required"]

    def test_keyboard_shortcut_schema(self):
        schema = KeyboardShortcutNode().get_params_schema()
        assert "key" in schema["required"]
        assert "modifiers" in schema["properties"]

    def test_computer_use_loop_schema(self):
        schema = ComputerUseLoopNode().get_params_schema()
        assert "task" in schema["required"]


class TestMouseMoveNode:
    @pytest.mark.asyncio
    @patch("fusion_desk.nodes.macos.input_nodes._try_pyobjc_move", return_value=True)
    async def test_move_success(self, mock_move):
        node = MouseMoveNode(config=NodeConfig(params={"x": 100, "y": 200}))
        result = await node.execute({})
        assert result.status == NodeStatus.SUCCESS
        assert result.data["x"] == 100

    @pytest.mark.asyncio
    @patch("fusion_desk.nodes.macos.input_nodes._try_pyobjc_move", return_value=False)
    @patch("fusion_desk.nodes.macos.input_nodes._applescript_move", return_value=(0, ""))
    async def test_move_applescript_fallback(self, mock_as, mock_pyobjc):
        node = MouseMoveNode(config=NodeConfig(params={"x": 50, "y": 80}))
        result = await node.execute({})
        assert result.status == NodeStatus.SUCCESS


class TestKeyboardTypeNode:
    @pytest.mark.asyncio
    @patch("fusion_desk.nodes.macos.input_nodes._try_pyobjc_type", return_value=True)
    async def test_type_success(self, mock_type):
        node = KeyboardTypeNode(config=NodeConfig(params={"text": "hello"}))
        result = await node.execute({})
        assert result.status == NodeStatus.SUCCESS
        assert result.data["char_count"] == 5

    @pytest.mark.asyncio
    async def test_type_empty_text(self):
        node = KeyboardTypeNode(config=NodeConfig(params={"text": ""}))
        result = await node.execute({})
        assert result.status == NodeStatus.FAILED


class TestKeyboardShortcutNode:
    @pytest.mark.asyncio
    @patch("fusion_desk.nodes.macos.input_nodes._applescript_key_code", return_value=(0, ""))
    async def test_shortcut_named_key(self, mock_kc):
        node = KeyboardShortcutNode(config=NodeConfig(params={"key": "enter", "modifiers": ["cmd"]}))
        result = await node.execute({})
        assert result.status == NodeStatus.SUCCESS

    @pytest.mark.asyncio
    @patch("fusion_desk.nodes.macos.input_nodes._applescript_key_code", return_value=(1, "err"))
    async def test_shortcut_failed(self, mock_kc):
        node = KeyboardShortcutNode(config=NodeConfig(params={"key": "enter"}))
        result = await node.execute({})
        assert result.status == NodeStatus.FAILED


class TestKeyMap:
    def test_key_map(self):
        assert _KEY_MAP["enter"] == 36
        assert _KEY_MAP["space"] == 49

    def test_mod_map(self):
        assert _MOD_MAP["cmd"] == "command"


# ── M4: 远程控制 ──

from fusion_desk.server.remote import RemoteControlServer, RemoteControlClient


class TestRemoteControlServer:
    def test_init(self):
        server = RemoteControlServer(host="0.0.0.0", port=9999, token="t1")
        assert server.host == "0.0.0.0"
        assert server.port == 9999
        assert server.token == "t1"

    def test_cancel_task(self):
        result = RemoteControlServer()._cancel_task("abc")
        assert result["task_id"] == "abc"

    @pytest.mark.asyncio
    async def test_auth_check(self):
        server = RemoteControlServer(token="secret")
        resp = await server._process_request("c1", {"method": "status", "id": 1})
        assert "error" in resp

    @pytest.mark.asyncio
    async def test_auth_pass(self):
        server = RemoteControlServer(token="secret")
        resp = await server._process_request("c1", {"method": "status", "id": 1, "token": "secret"})
        assert "result" in resp

    @pytest.mark.asyncio
    async def test_unknown_method(self):
        server = RemoteControlServer()
        resp = await server._process_request("c1", {"method": "nope", "id": 1})
        assert "error" in resp


class TestRemoteControlClient:
    def test_init(self):
        client = RemoteControlClient(token="tok")
        assert client.token == "tok"

    @pytest.mark.asyncio
    async def test_request_not_connected(self):
        client = RemoteControlClient()
        with pytest.raises(RuntimeError, match="Not connected"):
            await client._request("status")


# ── M4: 结构化输出 ──

class TestOutputSchema:
    def test_validate_object_pass(self):
        schema = {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}
        assert OutputSchema.validate({"name": "test"}, schema) is True

    def test_validate_object_missing_required(self):
        schema = {"type": "object", "required": ["name"]}
        assert OutputSchema.validate({}, schema) is False

    def test_validate_wrong_type(self):
        assert OutputSchema.validate(123, {"type": "string"}) is False

    def test_validate_array(self):
        schema = {"type": "array", "items": {"type": "integer"}}
        assert OutputSchema.validate([1, 2], schema) is True
        assert OutputSchema.validate([1, "x"], schema) is False

    def test_validate_no_type(self):
        assert OutputSchema.validate({"any": "data"}, {}) is True

    def test_validate_nested(self):
        schema = {"type": "object", "properties": {"u": {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]}}}
        assert OutputSchema.validate({"u": {"id": 1}}, schema) is True
        assert OutputSchema.validate({"u": {"id": "bad"}}, schema) is False

    def test_validate_detailed_errors(self):
        schema = {"type": "object", "required": ["name"]}
        errors = OutputSchema.validate_detailed({}, schema)
        assert len(errors) > 0


class TestNodeResultValidate:
    def test_no_schema(self):
        assert NodeResult(status=NodeStatus.SUCCESS, data={"x": 1}).validate() is True

    def test_schema_pass(self):
        schema = {"type": "object", "properties": {"x": {"type": "integer"}}, "required": ["x"]}
        assert NodeResult(status=NodeStatus.SUCCESS, data={"x": 1}, schema=schema).validate() is True

    def test_schema_fail(self):
        schema = {"type": "object", "properties": {"x": {"type": "integer"}}, "required": ["x"]}
        assert NodeResult(status=NodeStatus.SUCCESS, data={"x": "bad"}, schema=schema).validate() is False


# ── M4: CLI 命令 ──

class TestCLICommandsM4:
    def test_computer_use_group(self):
        from click.testing import CliRunner
        from fusion_desk.cli import cli
        result = CliRunner().invoke(cli, ["computer-use", "--help"])
        assert result.exit_code == 0
        assert "move" in result.output

    def test_remote_group(self):
        from click.testing import CliRunner
        from fusion_desk.cli import cli
        result = CliRunner().invoke(cli, ["remote", "--help"])
        assert result.exit_code == 0
        assert "serve" in result.output

    def test_schema_group(self):
        from click.testing import CliRunner
        from fusion_desk.cli import cli
        result = CliRunner().invoke(cli, ["schema", "--help"])
        assert result.exit_code == 0
        assert "validate" in result.output

    def test_schema_check_node(self):
        from click.testing import CliRunner
        from fusion_desk.cli import cli
        result = CliRunner().invoke(cli, ["schema", "check", "mouse_move"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "x" in data.get("properties", {})


class TestLazyImportsM4:
    def test_mouse_move_lazy(self):
        from fusion_desk import MouseMoveNode
        assert MouseMoveNode is not None

    def test_remote_server_lazy(self):
        from fusion_desk import RemoteControlServer
        assert RemoteControlServer is not None

    def test_output_schema_lazy(self):
        from fusion_desk import OutputSchema
        assert OutputSchema is not None

    def test_node_name_aliases_m4(self):
        from fusion_desk import NODE_NAME_ALIASES
        assert NODE_NAME_ALIASES.get("鼠标移动") == "mouse_move"
        assert NODE_NAME_ALIASES.get("Computer Use") == "computer_use_loop"
