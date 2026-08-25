"""M4 里程碑测试 — DeskRPC事件/会话/权限 + Computer Use + 远程控制 + 结构化输出。"""

import json
import os
import tempfile
import time
from unittest.mock import patch

import pytest

from fusion_cowork.engine.events import EventEmitter
from fusion_cowork.engine.hooks import HookManager
from fusion_cowork.engine.node import BaseNode, NodeConfig, NodeRegistry, NodeResult, NodeStatus
from fusion_cowork.engine.permission import PermissionLevel, PermissionManager
from fusion_cowork.engine.session import Session, SessionStore
from fusion_cowork.server.desk_rpc import DeskRPCServer


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
            permission_manager=pm,
            hook_manager=hm,
            event_emitter=em,
            session_store=store,
        )
        assert rpc._permission_manager is pm
        assert rpc._hook_manager is hm
        assert rpc._event_emitter is em
        assert rpc._session_store is store

    def test_handler_count(self):
        rpc = DeskRPCServer()
        assert len(rpc._handlers) >= 20


# ── DeskRPC nodes 序列化 (issue #40) ──


class TestDeskRPCNodesSerialization:
    """regression #40: desk.nodes.list / categories 曾返回 0 bytes (NodeCategory 不可序列化)。"""

    @pytest.mark.asyncio
    async def test_nodes_list_json_serializable(self):
        # 确保节点模块已注册
        import fusion_cowork.nodes.ai
        import fusion_cowork.nodes.io
        import fusion_cowork.nodes.logic
        import fusion_cowork.nodes.macos
        import fusion_cowork.nodes.tools  # noqa: F401

        rpc = DeskRPCServer()
        result = await rpc._handle_nodes_list({})
        assert result["count"] >= 1
        # 关键: 整个 result 必须可 JSON 序列化 (否则 _write_response 抛异常 → 0 bytes)
        serialized = json.dumps(result, ensure_ascii=False)
        decoded = json.loads(serialized)
        assert decoded["count"] == result["count"]
        for node in decoded["nodes"]:
            assert isinstance(node["category"], str)

    @pytest.mark.asyncio
    async def test_nodes_categories_json_serializable(self):
        import fusion_cowork.nodes.ai
        import fusion_cowork.nodes.io
        import fusion_cowork.nodes.logic
        import fusion_cowork.nodes.macos
        import fusion_cowork.nodes.tools  # noqa: F401

        rpc = DeskRPCServer()
        result = await rpc._handle_nodes_categories({})
        assert result["count"] >= 1
        serialized = json.dumps(result, ensure_ascii=False)
        decoded = json.loads(serialized)
        assert decoded["count"] == result["count"]
        for cat in decoded["categories"]:
            assert isinstance(cat, str)
            assert isinstance(decoded["categories"][cat], int)

    @pytest.mark.asyncio
    async def test_nodes_info_json_serializable(self):
        import fusion_cowork.nodes.io  # noqa: F401

        rpc = DeskRPCServer()
        # 取一个已注册节点名
        name = next(iter(NodeRegistry._registry.keys()))
        result = await rpc._handle_nodes_info({"name": name})
        assert "error" not in result
        serialized = json.dumps(result, ensure_ascii=False)
        decoded = json.loads(serialized)
        assert isinstance(decoded["category"], str)

    @pytest.mark.asyncio
    async def test_nodes_list_empty_registry_returns_empty(self):
        rpc = DeskRPCServer()
        saved = dict(NodeRegistry._registry)
        try:
            NodeRegistry._registry.clear()
            result = await rpc._handle_nodes_list({})
            assert result == {"nodes": [], "count": 0}
            # 空结果也必须可序列化
            json.dumps(result)
        finally:
            NodeRegistry._registry.update(saved)

    async def test_write_response_serialization_failure_sends_error_frame(self):
        """_write_response 遇不可序列化对象时应降级错误帧, 不静默断连。"""

        rpc = DeskRPCServer()

        class FakeWriter:
            def __init__(self):
                self.chunks = []

            def write(self, data):
                self.chunks.append(data)

            async def drain(self):
                return

            def get_extra_info(self, name):
                return None

        writer = FakeWriter()
        # 含 Enum 的 dict 不可 JSON 序列化
        bad = {"jsonrpc": "2.0", "id": 1, "result": {"cat": NodeStatus.SUCCESS}}
        await rpc._write_response(writer, bad)
        payload = b"".join(writer.chunks)
        assert len(payload) > 0
        frame = json.loads(payload.decode("utf-8"))
        assert frame["jsonrpc"] == "2.0"
        assert "error" in frame
        assert frame["error"]["code"] == -32603


# ── CLI 权限命令 ──


class TestCLIPermission:
    def test_permission_level_command(self):
        from click.testing import CliRunner

        from fusion_cowork.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["permission", "level", "auto"])
        assert result.exit_code == 0
        assert "auto" in result.output

    def test_permission_list_command(self):
        from click.testing import CliRunner

        from fusion_cowork.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["permission", "list"])
        assert result.exit_code == 0


# ── M4: Computer Use 输入节点 ──

from fusion_cowork.engine.schema import OutputSchema
from fusion_cowork.nodes.macos.input_nodes import (
    _KEY_MAP,
    _MOD_MAP,
    ComputerUseLoopNode,
    KeyboardShortcutNode,
    KeyboardTypeNode,
    MouseClickNode,
    MouseMoveNode,
)


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
    @patch("fusion_cowork.nodes.macos.input_nodes._try_pyobjc_move", return_value=True)
    async def test_move_success(self, mock_move):
        node = MouseMoveNode(config=NodeConfig(params={"x": 100, "y": 200}))
        result = await node.execute({})
        assert result.status == NodeStatus.SUCCESS
        assert result.data["x"] == 100

    @pytest.mark.asyncio
    @patch("fusion_cowork.nodes.macos.input_nodes._try_pyobjc_move", return_value=False)
    @patch("fusion_cowork.nodes.macos.input_nodes._applescript_move", return_value=(0, ""))
    async def test_move_applescript_fallback(self, mock_as, mock_pyobjc):
        node = MouseMoveNode(config=NodeConfig(params={"x": 50, "y": 80}))
        result = await node.execute({})
        assert result.status == NodeStatus.SUCCESS


class TestKeyboardTypeNode:
    @pytest.mark.asyncio
    @patch("fusion_cowork.nodes.macos.input_nodes._try_pyobjc_type", return_value=True)
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
    @patch("fusion_cowork.nodes.macos.input_nodes._applescript_key_code", return_value=(0, ""))
    async def test_shortcut_named_key(self, mock_kc):
        node = KeyboardShortcutNode(config=NodeConfig(params={"key": "enter", "modifiers": ["cmd"]}))
        result = await node.execute({})
        assert result.status == NodeStatus.SUCCESS

    @pytest.mark.asyncio
    @patch("fusion_cowork.nodes.macos.input_nodes._applescript_key_code", return_value=(1, "err"))
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

from fusion_cowork.server.remote import RemoteControlClient, RemoteControlServer


class TestRemoteControlServer:
    def test_init(self):
        server = RemoteControlServer(host="127.0.0.1", port=9999, token="t1")
        assert server.host == "127.0.0.1"
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


# ── P2-10: TLS + 命名会话 attach ──


class TestRemoteControlTLS:
    def test_build_ssl_context_no_cert_returns_none(self):
        server = RemoteControlServer()
        assert server._build_ssl_context() is None

    def test_build_ssl_context_bad_cert_fail_closed(self, tmp_path):
        cert = tmp_path / "cert.pem"
        key = tmp_path / "key.pem"
        cert.write_text("not a cert")
        key.write_text("not a key")
        server = RemoteControlServer(tls_cert=str(cert), tls_key=str(key))
        # HI-3 fail-closed: 配了证书但加载失败 → raise, 不降级明文
        with pytest.raises(RuntimeError, match="拒绝降级明文"):
            server._build_ssl_context()

    def test_build_ssl_context_valid_cert_returns_context(self, tmp_path):
        import subprocess

        cert = tmp_path / "cert.pem"
        key = tmp_path / "key.pem"
        # 生成自签名证书 (仅测试用)
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-keyout",
                str(key),
                "-out",
                str(cert),
                "-days",
                "1",
                "-nodes",
                "-subj",
                "/CN=localhost",
            ],
            check=True,
            capture_output=True,
        )
        server = RemoteControlServer(tls_cert=str(cert), tls_key=str(key))
        ctx = server._build_ssl_context()
        import ssl

        assert isinstance(ctx, ssl.SSLContext)
        assert ctx.minimum_version == ssl.TLSVersion.TLSv1_2


class TestRemoteControlAttachSession:
    @pytest.mark.asyncio
    async def test_attach_missing_session_id(self):
        server = RemoteControlServer()
        result = await server._attach_session("c1", "")
        assert "error" in result

    @pytest.mark.asyncio
    async def test_attach_nonexistent_session(self, monkeypatch, tmp_path):
        import fusion_cowork.engine.session as session_mod

        monkeypatch.setattr(session_mod, "DEFAULT_DB_PATH", str(tmp_path / "s.db"))
        server = RemoteControlServer()
        result = await server._attach_session("c1", "sess_does_not_exist")
        assert "error" in result

    @pytest.mark.asyncio
    async def test_attach_existing_session_returns_snapshot(self, monkeypatch, tmp_path):
        import fusion_cowork.engine.session as session_mod

        db_path = str(tmp_path / "s.db")
        monkeypatch.setattr(session_mod, "DEFAULT_DB_PATH", db_path)
        store = session_mod.SessionStore()
        sess = session_mod.Session(workflow_name="wf-attach", status="running")
        sess.steps_snapshot = [{"node": "file_input", "status": "success"}]
        store.save(sess)

        server = RemoteControlServer()
        result = await server._attach_session("c1", sess.id)
        assert result.get("attached") is True
        assert result["session_id"] == sess.id
        assert result["status"] == "running"
        assert result["workflow_name"] == "wf-attach"
        assert len(result["steps_snapshot"]) == 1
        assert server._session_attachments[sess.id] == "c1"


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
        schema = {
            "type": "object",
            "properties": {"u": {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]}},
        }
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

        from fusion_cowork.cli import cli

        result = CliRunner().invoke(cli, ["computer-use", "--help"])
        assert result.exit_code == 0
        assert "move" in result.output

    def test_remote_group(self):
        from click.testing import CliRunner

        from fusion_cowork.cli import cli

        result = CliRunner().invoke(cli, ["remote", "--help"])
        assert result.exit_code == 0
        assert "serve" in result.output

    def test_schema_group(self):
        from click.testing import CliRunner

        from fusion_cowork.cli import cli

        result = CliRunner().invoke(cli, ["schema", "--help"])
        assert result.exit_code == 0
        assert "validate" in result.output

    def test_schema_check_node(self):
        from click.testing import CliRunner

        from fusion_cowork.cli import cli

        result = CliRunner().invoke(cli, ["schema", "check", "mouse_move"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "x" in data.get("properties", {})


class TestLazyImportsM4:
    def test_mouse_move_lazy(self):
        from fusion_cowork import MouseMoveNode

        assert MouseMoveNode is not None

    def test_remote_server_lazy(self):
        from fusion_cowork import RemoteControlServer

        assert RemoteControlServer is not None

    def test_output_schema_lazy(self):
        from fusion_cowork import OutputSchema

        assert OutputSchema is not None

    def test_node_name_aliases_m4(self):
        from fusion_cowork import NODE_NAME_ALIASES

        assert NODE_NAME_ALIASES.get("鼠标移动") == "mouse_move"
        assert NODE_NAME_ALIASES.get("Computer Use") == "computer_use_loop"


class TestComputerUseVisionP0:
    # P0 回归: ComputerUseLoopNode 必须把截图以 image_url 多模态格式传给模型, 而非纯文本

    def test_build_vision_user_content_multimodal(self, tmp_path):
        from fusion_cowork.nodes.macos.input_nodes import _build_vision_user_content

        img = tmp_path / "shot.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\nfake-png-bytes")
        content = _build_vision_user_content("分析截图", str(img))

        assert isinstance(content, list)
        assert len(content) == 2
        assert content[0]["type"] == "text"
        assert content[0]["text"] == "分析截图"
        assert content[1]["type"] == "image_url"
        url = content[1]["image_url"]["url"]
        assert url.startswith("data:image/png;base64,")

    @pytest.mark.asyncio
    async def test_computer_use_loop_sends_image_to_model(self, tmp_path, monkeypatch):
        from fusion_cowork.ai.mlx_client import LLMResponse
        from fusion_cowork.engine.node import NodeConfig
        from fusion_cowork.nodes.macos.input_nodes import ComputerUseLoopNode

        # 伪截图文件
        shot = tmp_path / "cap.png"
        shot.write_bytes(b"\x89PNG\r\n\x1a\nfake")

        captured_messages = []

        class FakeCapture:
            def __init__(self, config=None):
                pass

            async def execute(self, inputs):
                return NodeResult(
                    status=NodeStatus.SUCCESS,
                    data={"path": str(shot)},
                )

        class FakeClient:
            async def chat(self, model, messages, **kwargs):
                captured_messages.append(messages)
                return LLMResponse(content="DONE")

        def fake_capture_factory(config=None):
            return FakeCapture()

        # ComputerUseLoopNode 在 execute 内做局部 import: from ...ai import FusionMLXClient
        # 和 from .system_nodes import ScreenCaptureNode, 故 patch 源模块
        import fusion_cowork.ai as ai_mod
        import fusion_cowork.nodes.macos.system_nodes as sys_mod

        monkeypatch.setattr(ai_mod, "FusionMLXClient", FakeClient, raising=False)
        monkeypatch.setattr(sys_mod, "ScreenCaptureNode", fake_capture_factory, raising=False)

        node = ComputerUseLoopNode(config=NodeConfig(params={"task": "打开 Safari", "max_steps": 1, "step_delay": 0}))
        result = await node.execute({})

        assert result.status == NodeStatus.SUCCESS
        assert len(captured_messages) == 1
        user_msg = captured_messages[0][0]
        assert user_msg["role"] == "user"
        content = user_msg["content"]
        assert isinstance(content, list), "P0 回归: content 必须是多模态列表, 不能是纯文本"
        types = [c["type"] for c in content]
        assert "image_url" in types, "P0 回归: 截图必须以 image_url 传给模型"
        assert "text" in types
