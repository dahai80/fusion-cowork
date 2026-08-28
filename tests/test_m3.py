"""M3 里程碑测试 — 插件系统 + 技能机制 + Chrome CDP + MCP权限拦截 + SSE + Session集成。"""

import asyncio
import json
import os
import tempfile
import time
import zipfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from fusion_cowork.engine.events import EventEmitter, EventType, WorkflowEvent
from fusion_cowork.engine.hooks import HookEvent, HookManager
from fusion_cowork.engine.node import BaseNode, NodeConfig, NodeRegistry, NodeResult, NodeStatus
from fusion_cowork.engine.permission import PermissionLevel, PermissionManager
from fusion_cowork.engine.session import SessionStore
from fusion_cowork.engine.workflow import Workflow, WorkflowEngine, WorkflowStatus
from fusion_cowork.plugins.manifest import PluginManifest
from fusion_cowork.server.mcp_server import MCPServer, MCPToolRegistry


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

    @pytest.mark.asyncio
    async def test_mcp_error_trace_id_no_leak(self):
        # E-9: 异常对外只返 trace_id + 通用消息, 不泄内部栈/路径/SQL
        pm = PermissionManager(level=PermissionLevel.BYPASS)
        registry = MCPToolRegistry(permission_manager=pm)
        registry.register_tools()

        class _BoomNode(BaseNode):
            name = "file_input"
            display_name = "File Input"

            def __init__(self, **kw):
                super().__init__(**kw)
                self.config = NodeConfig()

            async def execute(self, params):
                raise RuntimeError("internal SQL path /secret leak: SELECT * FROM users")

        saved = dict(NodeRegistry._registry)
        saved_aliases = dict(getattr(NodeRegistry, "_name_aliases", {}))
        NodeRegistry.clear()
        NodeRegistry.register(_BoomNode)

        try:
            result = await registry.call_tool("read_file", {"path": "/tmp/x"})
        finally:
            NodeRegistry._registry = saved
            NodeRegistry._name_aliases = saved_aliases
        assert result.get("isError") is True
        assert "_trace_id" in result
        body = result["content"][0]["text"]
        assert "trace_id" in body
        # 不泄漏内部栈/SQL/路径
        assert "SELECT" not in body
        assert "/secret" not in body
        assert "Traceback" not in body
        assert "RuntimeError" not in body

    @pytest.mark.asyncio
    async def test_mcp_run_workflow_maps_to_engine(self):
        # E-8: run_workflow 不再误映射 desktop_clean, 走 TemplateManager+WorkflowEngine
        pm = PermissionManager(level=PermissionLevel.BYPASS)
        registry = MCPToolRegistry(permission_manager=pm)
        registry.register_tools()
        # 不存在的模板 → 返回 error 含 template 字段, 不执行 desktop_clean
        result = await registry.call_tool("run_workflow", {"template": "__nope__"})
        body = json.loads(result["content"][0]["text"])
        assert body.get("template") == "__nope__"
        assert "error" in body


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
    async def test_session_resume_replays_from_snapshot(self):
        # P1-2 端到端: 执行 → 快照含 output_data → resume 跳过已完成节点续跑
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            store = SessionStore(db_path=db_path)
            engine = WorkflowEngine(session_store=store)
            wf = Workflow(name="resume_e2e", workflow_id="wf_res1")
            wf.add_node(_OkNode(node_id="n1"))
            await engine.execute(wf)

            sessions = store.list_sessions(limit=1)
            assert len(sessions) == 1
            # 快照步骤含 output_data (P1-2 修复)
            snap = sessions[0].steps_snapshot
            assert len(snap) >= 1
            assert snap[0]["output_data"] == {"content": "ok"}

            # resume 返回快照, 用作断点续跑输入
            payload = store.resume(sessions[0].id)
            assert payload is not None
            assert payload["steps_snapshot"] == snap

            # 用快照续跑: n1 应被跳过
            wf2 = Workflow(name="resume_e2e", workflow_id="wf_res1")
            wf2.add_node(_OkNode(node_id="n1"))
            execution = await engine.execute(wf2, resume_steps=payload["steps_snapshot"])
            assert execution.status == WorkflowStatus.SUCCESS
            assert execution.steps[0].status == NodeStatus.SKIPPED
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
                permission_manager=pm,
                hook_manager=hm,
                session_store=store,
                event_emitter=em,
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
        # E-9: 默认无注入时启用 CONFIRM 级 PermissionManager — 高风险 MCP 工具
        # (run_terminal/file_output/app_lifecycle) 不再无 gate 直跑。
        assert server._registry._permission_manager is not None
        assert server._registry._hook_manager is None


# ── 插件系统测试 ──


class TestPluginManifest:
    def test_create_manifest(self):
        from fusion_cowork.plugins.manifest import PluginManifest

        m = PluginManifest(
            name="test_plugin",
            version="1.0.0",
            description="A test plugin",
            author="test",
            nodes=["test_node"],
            entry_point="plugin",
        )
        assert m.name == "test_plugin"
        assert m.version == "1.0.0"
        assert m.nodes == ["test_node"]

    def test_manifest_to_dict(self):
        from fusion_cowork.plugins.manifest import PluginManifest

        m = PluginManifest(name="p1", version="0.1.0", description="d", nodes=["n1"])
        d = m.to_dict()
        assert d["name"] == "p1"
        assert d["version"] == "0.1.0"
        assert "nodes" in d

    def test_manifest_from_dict(self):
        from fusion_cowork.plugins.manifest import PluginManifest

        data = {"name": "p2", "version": "2.0", "description": "x", "nodes": ["a", "b"]}
        m = PluginManifest.from_dict(data)
        assert m.name == "p2"
        assert len(m.nodes) == 2

    def test_manifest_from_json_file(self):
        from fusion_cowork.plugins.manifest import PluginManifest

        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = Path(tmp) / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "name": "file_plugin",
                        "version": "0.3",
                        "description": "from file",
                        "nodes": ["x"],
                    }
                )
            )
            m = PluginManifest.from_json(manifest_path)
            assert m.name == "file_plugin"


class TestPluginLoader:
    def test_discover_empty(self):
        from fusion_cowork.plugins.loader import PluginLoader

        loader = PluginLoader()
        with tempfile.TemporaryDirectory() as tmp:
            loader._plugins_dir = Path(tmp)
            result = loader.discover()
            assert result == []

    def test_discover_with_plugin(self):
        from fusion_cowork.plugins.loader import PluginLoader
        from fusion_cowork.plugins.manifest import PluginManifest

        loader = PluginLoader()
        with tempfile.TemporaryDirectory() as tmp:
            plugin_dir = Path(tmp) / "my_plugin"
            plugin_dir.mkdir()
            manifest = PluginManifest(name="my_plugin", version="1.0", description="t", nodes=["n1"])
            (plugin_dir / "manifest.json").write_text(json.dumps(manifest.to_dict()))
            loader._plugins_dir = Path(tmp)
            result = loader.discover()
            assert len(result) == 1
            assert result[0].name == "my_plugin"
            assert result[0].version == "1.0"

    def test_install_from_dir(self):
        from fusion_cowork.plugins.loader import PluginLoader
        from fusion_cowork.plugins.manifest import PluginManifest

        loader = PluginLoader()
        with tempfile.TemporaryDirectory() as tmp_plugins, tempfile.TemporaryDirectory() as tmp_src:
            loader._plugins_dir = Path(tmp_plugins)
            src_dir = Path(tmp_src) / "src_plugin"
            src_dir.mkdir()
            manifest = PluginManifest(name="src_plugin", version="0.1", description="s", nodes=["n1"])
            (src_dir / "manifest.json").write_text(json.dumps(manifest.to_dict()))
            (src_dir / "plugin.py").write_text("# plugin code\n")
            result = loader.install(str(src_dir))
            assert result is True
            assert (Path(tmp_plugins) / "src_plugin").exists()

    def test_install_from_zip(self):
        from fusion_cowork.plugins.loader import PluginLoader
        from fusion_cowork.plugins.manifest import PluginManifest

        loader = PluginLoader()
        with tempfile.TemporaryDirectory() as tmp_plugins, tempfile.TemporaryDirectory() as tmp_src:
            loader._plugins_dir = Path(tmp_plugins)
            src_dir = Path(tmp_src) / "zip_plugin"
            src_dir.mkdir()
            manifest = PluginManifest(name="zip_plugin", version="0.2", description="z", nodes=["n1"])
            (src_dir / "manifest.json").write_text(json.dumps(manifest.to_dict()))
            (src_dir / "plugin.py").write_text("# plugin\n")
            zip_path = Path(tmp_src) / "zip_plugin.zip"
            with zipfile.ZipFile(zip_path, "w") as zf:
                for f in src_dir.iterdir():
                    zf.write(f, f"zip_plugin/{f.name}")
            result = loader.install(str(zip_path))
            assert result is True
            assert (Path(tmp_plugins) / "zip_plugin").exists()

    def test_uninstall(self):
        from fusion_cowork.plugins.loader import PluginLoader
        from fusion_cowork.plugins.manifest import PluginManifest

        loader = PluginLoader()
        with tempfile.TemporaryDirectory() as tmp:
            loader._plugins_dir = Path(tmp)
            plugin_dir = Path(tmp) / "rm_plugin"
            plugin_dir.mkdir()
            manifest = PluginManifest(name="rm_plugin", version="1.0", description="r", nodes=[])
            (plugin_dir / "manifest.json").write_text(json.dumps(manifest.to_dict()))
            ok = loader.uninstall("rm_plugin")
            assert ok is True
            assert not plugin_dir.exists()


# ── E-13: 插件清单/zip 安装安全加固 ──


class TestPluginManifestNameValidation:
    # E-13: from_dict 拒绝空 name (防 target=plugins_dir 越界删除) 与路径分隔符 (防遍历)

    def test_empty_name_rejected(self):
        from fusion_cowork.plugins.manifest import PluginManifest

        with pytest.raises(ValueError):
            PluginManifest.from_dict({"name": "", "version": "0.1"})

    def test_whitespace_only_name_rejected(self):
        from fusion_cowork.plugins.manifest import PluginManifest

        with pytest.raises(ValueError):
            PluginManifest.from_dict({"name": "   ", "version": "0.1"})

    def test_missing_name_rejected(self):
        from fusion_cowork.plugins.manifest import PluginManifest

        with pytest.raises(ValueError):
            PluginManifest.from_dict({"version": "0.1"})

    def test_path_separator_name_rejected(self):
        from fusion_cowork.plugins.manifest import PluginManifest

        for bad in ["a/b", "a\\b", ".", ".."]:
            with pytest.raises(ValueError):
                PluginManifest.from_dict({"name": bad, "version": "0.1"})

    def test_valid_name_accepted(self):
        from fusion_cowork.plugins.manifest import PluginManifest

        m = PluginManifest.from_dict({"name": "ok_plugin-1", "version": "0.1"})
        assert m.name == "ok_plugin-1"


class TestSafeRmtreeBaseGuard:
    # E-13: _safe_rmtree 拒绝 target==base (name="" 或 ".") — 旧版 rmtree(plugins_dir) 删插件根

    def test_empty_name_rejected(self):
        from fusion_cowork.plugins.loader import PluginLoader

        loader = PluginLoader()
        with tempfile.TemporaryDirectory() as tmp:
            loader._plugins_dir = Path(tmp)
            ok = loader._safe_rmtree("")
            assert ok is False
            assert Path(tmp).exists(), "插件根目录必须保留"

    def test_dot_name_rejected(self):
        from fusion_cowork.plugins.loader import PluginLoader

        loader = PluginLoader()
        with tempfile.TemporaryDirectory() as tmp:
            loader._plugins_dir = Path(tmp)
            ok = loader._safe_rmtree(".")
            assert ok is False
            assert Path(tmp).exists()

    def test_normal_plugin_deleted(self):
        from fusion_cowork.plugins.loader import PluginLoader

        loader = PluginLoader()
        with tempfile.TemporaryDirectory() as tmp:
            loader._plugins_dir = Path(tmp)
            pdir = Path(tmp) / "real_plugin"
            pdir.mkdir()
            ok = loader._safe_rmtree("real_plugin")
            assert ok is True
            assert not pdir.exists()

    def test_traversal_rejected(self):
        from fusion_cowork.plugins.loader import PluginLoader

        loader = PluginLoader()
        with tempfile.TemporaryDirectory() as tmp:
            loader._plugins_dir = Path(tmp)
            ok = loader._safe_rmtree("../escape")
            assert ok is False


class TestInstallZipBombGuard:
    # E-13: _install_zip 限文件数/解压总大小/压缩比, 防 zip 炸弹

    def _make_zip(self, tmp_src: str, entries: dict) -> Path:
        zip_path = Path(tmp_src) / "bomb.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, content in entries.items():
                zf.writestr(name, content)
        return zip_path

    def test_too_many_entries_rejected(self):
        from fusion_cowork.plugins.loader import PluginLoader

        loader = PluginLoader()
        # 10001 条目 > 上限 10000
        entries = {f"bomb/f{i}.txt": "x" for i in range(10001)}
        with tempfile.TemporaryDirectory() as tmp_plugins, tempfile.TemporaryDirectory() as tmp_src:
            loader._plugins_dir = Path(tmp_plugins)
            zip_path = self._make_zip(tmp_src, entries)
            ok = loader._install_zip(zip_path)
            assert ok is False

    def test_high_compression_ratio_rejected(self):
        from fusion_cowork.plugins.loader import PluginLoader

        loader = PluginLoader()
        # 1KB 压缩 → ~1MB 解压, 比 >200 (但解压总大小 <512MB, 走压缩比分支)
        entries = {"bomb/big.txt": "A" * (1024 * 1024), "bomb/manifest.json": "{}"}
        with tempfile.TemporaryDirectory() as tmp_plugins, tempfile.TemporaryDirectory() as tmp_src:
            loader._plugins_dir = Path(tmp_plugins)
            zip_path = self._make_zip(tmp_src, entries)
            ok = loader._install_zip(zip_path)
            assert ok is False, "高压缩比 zip 应被拒绝"

    def test_normal_zip_accepted(self):
        from fusion_cowork.plugins.loader import PluginLoader
        from fusion_cowork.plugins.manifest import PluginManifest

        loader = PluginLoader()
        manifest = PluginManifest(name="ok_zip", version="0.1", description="d", nodes=["n1"])
        entries = {
            "ok_zip/manifest.json": json.dumps(manifest.to_dict()),
            "ok_zip/plugin.py": "# plugin\n",
        }
        with tempfile.TemporaryDirectory() as tmp_plugins, tempfile.TemporaryDirectory() as tmp_src:
            loader._plugins_dir = Path(tmp_plugins)
            zip_path = self._make_zip(tmp_src, entries)
            ok = loader._install_zip(zip_path)
            assert ok is True
            assert (Path(tmp_plugins) / "ok_zip").exists()


# ── P1-6 插件沙箱运行时隔离测试 ──

_SANDBOX_PLUGIN_CODE = """
from fusion_cowork.engine.node import BaseNode, NodeCategory, NodeResult, NodeStatus, register_node

@register_node
class EchoNode(BaseNode):
    name = "sbx_echo"
    display_name = "Sbx Echo"
    category = NodeCategory.TOOL
    description = "echo with prefix"
    icon = "🔁"
    default_label = "回声"
    def get_params_schema(self):
        return {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}
    async def execute(self, inputs):
        return NodeResult(
            status=NodeStatus.SUCCESS,
            data={"echo": "SBX:" + str(inputs.get("text", ""))},
            summary="echoed",
        )
"""


def _make_sandbox_plugin(tmp: str) -> Path:
    pdir = Path(tmp) / "sbx_plugin"
    pdir.mkdir()
    (pdir / "plugin.py").write_text(_SANDBOX_PLUGIN_CODE)
    manifest = PluginManifest(
        name="sbx_plugin",
        version="0.1",
        description="d",
        nodes=["sbx_echo"],
        entry_point="plugin",
        sandbox=True,
    )
    (pdir / "manifest.json").write_text(json.dumps(manifest.to_dict()))
    return pdir


class TestPluginSandboxIsolation:
    def teardown_method(self, method):
        NodeRegistry.unregister("sbx_echo")

    def test_load_sandboxed_registers_wrapper_not_raw(self):
        from fusion_cowork.plugins.loader import PluginLoader, SandboxedNode

        with tempfile.TemporaryDirectory() as tmp:
            _make_sandbox_plugin(tmp)
            loader = PluginLoader()
            loader._plugins_dir = Path(tmp)
            nodes = loader.load("sbx_plugin")
            assert len(nodes) == 1
            assert nodes[0].name == "sbx_echo"
            assert issubclass(nodes[0], SandboxedNode)
            assert nodes[0].__name__.startswith("SandboxedNode_")

    def test_sandboxed_node_execute_runs_out_of_process(self):
        from fusion_cowork.plugins.loader import PluginLoader

        with tempfile.TemporaryDirectory() as tmp:
            _make_sandbox_plugin(tmp)
            loader = PluginLoader()
            loader._plugins_dir = Path(tmp)
            loader.load("sbx_plugin")
            cls = NodeRegistry.get("sbx_echo")
            assert cls is not None
            inst = cls(node_id="t1")
            schema = inst.get_params_schema()
            assert "text" in schema.get("properties", {})
            res = asyncio.run(inst.execute({"text": "hello"}))
            assert res.status == NodeStatus.SUCCESS
            assert res.data == {"echo": "SBX:hello"}

    def test_sandboxed_node_failure_propagates(self):
        crash_code = _SANDBOX_PLUGIN_CODE.replace(
            'return NodeResult(\n            status=NodeStatus.SUCCESS,\n            data={"echo": "SBX:" + str(inputs.get("text", ""))},\n            summary="echoed",\n        )',
            'raise RuntimeError("plugin boom")',
        )
        with tempfile.TemporaryDirectory() as tmp:
            pdir = Path(tmp) / "sbx_plugin"
            pdir.mkdir()
            (pdir / "plugin.py").write_text(crash_code)
            manifest = PluginManifest(
                name="sbx_plugin",
                version="0.1",
                description="d",
                nodes=["sbx_echo"],
                entry_point="plugin",
                sandbox=True,
            )
            (pdir / "manifest.json").write_text(json.dumps(manifest.to_dict()))
            from fusion_cowork.plugins.loader import PluginLoader

            loader = PluginLoader()
            loader._plugins_dir = Path(tmp)
            nodes = loader.load("sbx_plugin")
            assert len(nodes) == 1, "introspect should succeed even though execute crashes"
            cls = NodeRegistry.get("sbx_echo")
            inst = cls(node_id="t2")
            res = asyncio.run(inst.execute({"text": "x"}))
            assert res.status == NodeStatus.FAILED
            assert "boom" in (res.error or "")
            NodeRegistry.unregister("sbx_echo")

    def test_sandboxed_node_empty_input_fails_gracefully(self):
        from fusion_cowork.plugins.loader import SandboxedNode

        node = SandboxedNode(node_id="empty", entry_file="", class_name="")
        res = asyncio.run(node.execute({}))
        assert res.status == NodeStatus.FAILED
        assert "entry_file" in (res.error or "")


# ── 技能机制测试 ──


class TestSkillRegistry:
    @pytest.fixture(autouse=True)
    def _cleanup_skills(self):
        from fusion_cowork.skills.registry import SkillRegistry

        yield
        SkillRegistry()._skills.clear()

    def test_register_and_get(self):
        from fusion_cowork.skills.registry import Skill, SkillRegistry

        registry = SkillRegistry()
        handler = MagicMock()
        registry.register(Skill(name="test_skill_m3", description="test", handler=handler))
        s = registry.get("test_skill_m3")
        assert s is not None
        assert s.name == "test_skill_m3"

    def test_register_with_aliases(self):
        from fusion_cowork.skills.registry import Skill, SkillRegistry

        registry = SkillRegistry()
        handler = MagicMock()
        registry.register(Skill(name="cleanup_m3", description="clean", handler=handler, aliases=["/clean_m3"]))
        s = registry.get("/clean_m3")
        assert s is not None
        assert s.name == "cleanup_m3"

    def test_unregister(self):
        from fusion_cowork.skills.registry import Skill, SkillRegistry

        registry = SkillRegistry()
        handler = MagicMock()
        registry.register(Skill(name="rm_me_m3", description="x", handler=handler))
        registry.unregister("rm_me_m3")
        assert registry.get("rm_me_m3") is None

    def test_list_skills(self):
        from fusion_cowork.skills.registry import Skill, SkillRegistry

        registry = SkillRegistry()
        handler = MagicMock()
        registry.register(Skill(name="list_a_m3", description="a", handler=handler))
        registry.register(Skill(name="list_b_m3", description="b", handler=handler))
        skills = registry.list_skills()
        names = [s.name for s in skills]
        assert "list_a_m3" in names
        assert "list_b_m3" in names

    def test_search(self):
        from fusion_cowork.skills.registry import Skill, SkillRegistry

        registry = SkillRegistry()
        handler = MagicMock()
        registry.register(
            Skill(name="screenshot_m3", description="take screenshot", handler=handler, category="visual")
        )
        results = registry.search("screenshot_m3")
        assert len(results) >= 1

    def test_execute(self):
        from fusion_cowork.skills.registry import Skill, SkillRegistry

        called = []

        async def handler(args=""):
            called.append(True)
            return {"ok": True}

        registry = SkillRegistry()
        registry.register(Skill(name="exec_test_m3", description="t", handler=handler))
        result = asyncio.run(registry.execute("exec_test_m3"))
        assert len(called) == 1
        assert result == {"ok": True}

    def test_execute_not_found(self):
        from fusion_cowork.skills.registry import SkillRegistry

        registry = SkillRegistry()
        result = asyncio.run(registry.execute("nonexistent_m3"))
        assert "error" in result

    def test_clear(self):
        from fusion_cowork.skills.registry import Skill, SkillRegistry

        registry = SkillRegistry()
        handler = MagicMock()
        registry.register(Skill(name="clear_x_m3", description="x", handler=handler))
        registry.clear()
        assert registry.list_skills() == []


# ── E-13: 基线内置节点卸载保护 ──


class TestBuiltinNodeUnregisterGuard:
    def test_freeze_then_unregister_builtin_rejected(self):
        # E-13: freeze_builtins 后, 内置名不可被 unregister 删除
        # (防恶意插件 node_map 谎报拥有 shell_exec → loader.unload 删内置)
        from fusion_cowork.engine.node import BaseNode, NodeConfig, NodeRegistry

        class _E13FakeBuiltin(BaseNode):
            name = "e13_builtin_node"
            display_name = "E13 Builtin"

            def __init__(self, **kw):
                super().__init__(**kw)
                self.config = NodeConfig()

            async def execute(self, params):
                return {"status": "ok"}

        saved = dict(NodeRegistry._registry)
        saved_builtins = set(NodeRegistry._builtin_names)
        saved_frozen = NodeRegistry._builtins_frozen
        try:
            NodeRegistry.register(_E13FakeBuiltin)
            # 模拟 import_all_nodes: 仅冻结本次模块新注册的名 (非全表快照)
            NodeRegistry.freeze_builtins(extra_names={"e13_builtin_node"})
            assert "e13_builtin_node" in NodeRegistry._builtin_names
            # 卸载被拒 — 内置名受保护
            NodeRegistry.unregister("e13_builtin_node")
            assert "e13_builtin_node" in NodeRegistry._registry
            # force=True 仍可卸 (内部重注册场景)
            NodeRegistry.unregister("e13_builtin_node", force=True)
            assert "e13_builtin_node" not in NodeRegistry._registry
        finally:
            NodeRegistry._registry = saved
            NodeRegistry._builtin_names = saved_builtins
            NodeRegistry._builtins_frozen = saved_frozen

    def test_unregister_non_builtin_ok(self):
        # E-13: freeze 后注册的插件节点不在 builtin 快照 → 可正常卸载 (插件生命周期)
        from fusion_cowork.engine.node import BaseNode, NodeConfig, NodeRegistry

        class _E13PluginNode(BaseNode):
            name = "e13_plugin_node"
            display_name = "E13 Plugin"

            def __init__(self, **kw):
                super().__init__(**kw)
                self.config = NodeConfig()

            async def execute(self, params):
                return {"status": "ok"}

        saved = dict(NodeRegistry._registry)
        saved_builtins = set(NodeRegistry._builtin_names)
        saved_frozen = NodeRegistry._builtins_frozen
        try:
            NodeRegistry.freeze_builtins()
            builtin_count = len(NodeRegistry._builtin_names)
            # freeze 之后注册的插件节点不在快照内
            NodeRegistry.register(_E13PluginNode)
            assert "e13_plugin_node" not in NodeRegistry._builtin_names
            assert len(NodeRegistry._builtin_names) == builtin_count
            NodeRegistry.unregister("e13_plugin_node")
            assert "e13_plugin_node" not in NodeRegistry._registry
        finally:
            NodeRegistry._registry = saved
            NodeRegistry._builtin_names = saved_builtins
            NodeRegistry._builtins_frozen = saved_frozen


class TestBuiltinSkills:
    @pytest.fixture(autouse=True)
    def _cleanup_skills(self):
        from fusion_cowork.skills.registry import SkillRegistry

        yield
        SkillRegistry()._skills.clear()

    def test_register_builtin_skills(self):
        from fusion_cowork.skills.builtin import register_builtin_skills
        from fusion_cowork.skills.registry import SkillRegistry

        SkillRegistry()._skills.clear()
        registry = SkillRegistry()
        register_builtin_skills(registry)
        skills = registry.list_skills()
        assert len(skills) >= 6

    def test_builtin_skill_names(self):
        from fusion_cowork.skills.builtin import BUILTIN_SKILLS, register_builtin_skills
        from fusion_cowork.skills.registry import SkillRegistry

        SkillRegistry()._skills.clear()
        registry = SkillRegistry()
        register_builtin_skills(registry)
        for skill_obj in BUILTIN_SKILLS:
            s = registry.get(skill_obj.name)
            assert s is not None, f"Missing skill: {skill_obj.name}"


# ── CDP 测试 ──


class TestCDPClient:
    def test_init(self):
        from fusion_cowork.nodes.browser.cdp_client import CDPClient

        client = CDPClient(host="127.0.0.1", port=9222)
        assert client.host == "127.0.0.1"
        assert client.port == 9222

    def test_missing_deps_graceful(self):
        from fusion_cowork.nodes.browser.cdp_client import CDPClient

        client = CDPClient()
        assert client is not None


# ── issue #65: FUSION_BROWSER_CDP env 目标切换 ──


class TestCDPFusionBrowserEnvSwitch:
    def test_env_set_targets_fusion_browser(self, monkeypatch):
        from fusion_cowork.nodes.browser.cdp_client import CDPClient

        monkeypatch.setenv("FUSION_BROWSER_CDP", "9222")
        client = CDPClient(host="127.0.0.1", port=9999, token="x")
        assert client.host == "127.0.0.1"
        assert client.port == 9222
        # issue #72: fusion-browser WS upgrade 需 Bearer, token 保留 (非置 None) 以转发
        assert client.token == "x"
        assert client._target == "fusion-browser"

    def test_env_unset_keeps_chrome_path(self, monkeypatch):
        from fusion_cowork.nodes.browser.cdp_client import CDPClient

        monkeypatch.delenv("FUSION_BROWSER_CDP", raising=False)
        client = CDPClient(host="127.0.0.1", port=9222, token="sek")
        assert client.host == "127.0.0.1"
        assert client.port == 9222
        assert client.token == "sek"
        assert client._target == "chrome"

    def test_env_invalid_value_falls_back(self, monkeypatch):
        from fusion_cowork.nodes.browser.cdp_client import CDPClient

        monkeypatch.setenv("FUSION_BROWSER_CDP", "not-a-port")
        client = CDPClient(host="127.0.0.1", port=9222)
        assert client._target == "chrome"
        assert client.port == 9222

    def test_env_out_of_range_falls_back(self, monkeypatch):
        from fusion_cowork.nodes.browser.cdp_client import CDPClient

        monkeypatch.setenv("FUSION_BROWSER_CDP", "70000")
        client = CDPClient(host="127.0.0.1", port=9222)
        assert client._target == "chrome"

    def test_env_skips_localhost_token_warnings(self, monkeypatch, caplog):
        import logging

        from fusion_cowork.nodes.browser.cdp_client import CDPClient

        monkeypatch.setenv("FUSION_BROWSER_CDP", "9222")
        with caplog.at_level(logging.WARNING):
            client = CDPClient(host="8.8.8.8", port=9222)
        assert client.host == "127.0.0.1"
        assert all("未配置 token" not in r.message for r in caplog.records)


# ── issue #72: CDP WS upgrade 要求 Authorization: Bearer ──


class TestCDPWSBearerForwarding:
    @pytest.mark.asyncio
    async def test_ws_connect_forwards_bearer_with_token(self, monkeypatch):
        import fusion_cowork.nodes.browser.cdp_client as mod

        captured = {}

        class FakeWS:
            async def recv(self):
                raise asyncio.CancelledError

            async def close(self):
                pass

        async def fake_connect(uri, **kwargs):
            captured["uri"] = uri
            captured["kwargs"] = kwargs
            return FakeWS()

        monkeypatch.setattr(mod.websockets, "connect", fake_connect)
        monkeypatch.delenv("FUSION_BROWSER_CDP", raising=False)

        client = mod.CDPClient(host="127.0.0.1", port=9222, token="sek")

        async def fake_get_ws_url(self):
            return "ws://127.0.0.1:9222/devtools/page/x"

        monkeypatch.setattr(mod.CDPClient, "_get_ws_url", fake_get_ws_url)
        await client.connect()
        assert captured["kwargs"].get("additional_headers") == {"Authorization": "Bearer sek"}
        await client.disconnect()

    @pytest.mark.asyncio
    async def test_ws_connect_no_header_without_token(self, monkeypatch):
        import fusion_cowork.nodes.browser.cdp_client as mod

        captured = {}

        class FakeWS:
            async def recv(self):
                raise asyncio.CancelledError

            async def close(self):
                pass

        async def fake_connect(uri, **kwargs):
            captured["kwargs"] = kwargs
            return FakeWS()

        monkeypatch.setattr(mod.websockets, "connect", fake_connect)
        monkeypatch.delenv("FUSION_BROWSER_CDP", raising=False)

        client = mod.CDPClient(host="127.0.0.1", port=9222, token=None)

        async def fake_get_ws_url(self):
            return "ws://127.0.0.1:9222/devtools/page/x"

        monkeypatch.setattr(mod.CDPClient, "_get_ws_url", fake_get_ws_url)
        await client.connect()
        assert "additional_headers" not in captured["kwargs"]
        await client.disconnect()


class TestCDPNodes:
    @pytest.fixture(autouse=True)
    def _register(self):
        import fusion_cowork.nodes.browser.cdp_nodes  # noqa: F401

    def test_cdp_navigate_registered(self):
        node = NodeRegistry.get("cdp_navigate")
        assert node is not None

    def test_cdp_snapshot_registered(self):
        node = NodeRegistry.get("cdp_snapshot")
        assert node is not None

    def test_cdp_click_registered(self):
        node = NodeRegistry.get("cdp_click")
        assert node is not None

    def test_cdp_fill_registered(self):
        node = NodeRegistry.get("cdp_fill")
        assert node is not None

    def test_cdp_fill_form_registered(self):
        node = NodeRegistry.get("cdp_fill_form")
        assert node is not None

    def test_cdp_screenshot_registered(self):
        node = NodeRegistry.get("cdp_screenshot")
        assert node is not None

    def test_cdp_evaluate_registered(self):
        node = NodeRegistry.get("cdp_evaluate")
        assert node is not None

    def test_cdp_emulate_registered(self):
        node = NodeRegistry.get("cdp_emulate")
        assert node is not None

    def test_cdp_network_registered(self):
        node = NodeRegistry.get("cdp_network")
        assert node is not None

    def test_cdp_console_registered(self):
        node = NodeRegistry.get("cdp_console")
        assert node is not None

    def test_cdp_navigate_no_url(self):
        node = NodeRegistry.create("cdp_navigate", config=NodeConfig(params={}))
        result = asyncio.run(node.execute({}))
        assert result.status == NodeStatus.FAILED

    def test_cdp_fill_missing_params(self):
        node = NodeRegistry.create("cdp_fill", config=NodeConfig(params={}))
        result = asyncio.run(node.execute({}))
        assert result.status == NodeStatus.FAILED

    def test_cdp_fill_form_missing_fields(self):
        node = NodeRegistry.create("cdp_fill_form", config=NodeConfig(params={}))
        result = asyncio.run(node.execute({}))
        assert result.status == NodeStatus.FAILED

    def test_cdp_evaluate_no_script(self):
        node = NodeRegistry.create("cdp_evaluate", config=NodeConfig(params={}))
        result = asyncio.run(node.execute({}))
        assert result.status == NodeStatus.FAILED

    def test_cdp_click_no_node_id(self):
        node = NodeRegistry.create("cdp_click", config=NodeConfig(params={}))
        result = asyncio.run(node.execute({}))
        assert result.status == NodeStatus.FAILED

    def test_cdp_navigate_schema(self):
        node = NodeRegistry.get("cdp_navigate")
        inst = node(config=NodeConfig(params={}))
        schema = inst.get_params_schema()
        assert "url" in schema.get("properties", {})
        assert "url" in schema.get("required", [])

    def test_cdp_emulate_schema(self):
        node = NodeRegistry.get("cdp_emulate")
        inst = node(config=NodeConfig(params={}))
        schema = inst.get_params_schema()
        assert "width" in schema.get("properties", {})
        assert "height" in schema.get("properties", {})

    def test_cdp_screenshot_schema(self):
        node = NodeRegistry.get("cdp_screenshot")
        inst = node(config=NodeConfig(params={}))
        schema = inst.get_params_schema()
        assert "save_path" in schema.get("properties", {})


# ── MCP Skill 工具测试 ──


class TestMCPSkillTools:
    def test_skill_list_registered(self):
        from fusion_cowork.server.mcp_server import MCPToolRegistry

        reg = MCPToolRegistry()
        reg.register_tools()
        tools = reg.list_tools()
        names = [t["name"] for t in tools]
        assert "skill_list" in names
        assert "skill_run" in names

    def test_skill_list_execution(self):
        from fusion_cowork.server.mcp_server import MCPToolRegistry

        reg = MCPToolRegistry()
        reg.register_tools()
        result = asyncio.run(reg.call_tool("skill_list", {}))
        assert "content" in result
        data = json.loads(result["content"][0]["text"])
        assert "skills" in data
        assert data["count"] >= 0


# ── CLI 命令测试 ──


class TestCLICommandsM3:
    def test_plugin_group_exists(self):
        from click.testing import CliRunner

        from fusion_cowork.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["plugin", "--help"])
        assert result.exit_code == 0
        assert "插件管理" in result.output

    def test_skill_group_exists(self):
        from click.testing import CliRunner

        from fusion_cowork.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["skill", "--help"])
        assert result.exit_code == 0
        assert "技能管理" in result.output

    def test_cdp_group_exists(self):
        from click.testing import CliRunner

        from fusion_cowork.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["cdp", "--help"])
        assert result.exit_code == 0
        assert "Chrome" in result.output

    def test_plugin_list_command(self):
        from click.testing import CliRunner

        from fusion_cowork.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["plugin", "list"])
        # May show "没有发现插件" which is fine
        assert result.exit_code == 0 or "插件" in result.output

    def test_skill_list_command(self):
        from click.testing import CliRunner

        from fusion_cowork.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["skill", "list"])
        assert result.exit_code == 0

    def test_skill_search_command(self):
        from click.testing import CliRunner

        from fusion_cowork.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["skill", "search", "clean"])
        assert result.exit_code == 0
