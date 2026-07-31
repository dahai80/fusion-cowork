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

from fusion_cowork.engine.permission import PermissionManager, PermissionLevel
from fusion_cowork.engine.hooks import HookManager, HookEvent
from fusion_cowork.engine.session import Session, SessionStore
from fusion_cowork.engine.events import EventType, WorkflowEvent, EventEmitter
from fusion_cowork.engine.node import BaseNode, NodeConfig, NodeResult, NodeStatus, NodeRegistry
from fusion_cowork.engine.workflow import Workflow, WorkflowEngine, WorkflowStatus
from fusion_cowork.server.mcp_server import MCPToolRegistry, MCPServer


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


# ── 插件系统测试 ──


class TestPluginManifest:
    def test_create_manifest(self):
        from fusion_cowork.plugins.manifest import PluginManifest
        m = PluginManifest(
            name="test_plugin", version="1.0.0",
            description="A test plugin", author="test",
            nodes=["test_node"], entry_point="plugin",
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
            manifest_path.write_text(json.dumps({
                "name": "file_plugin", "version": "0.3",
                "description": "from file", "nodes": ["x"],
            }))
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
        registry.register(Skill(name="screenshot_m3", description="take screenshot", handler=handler, category="visual"))
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


class TestBuiltinSkills:
    @pytest.fixture(autouse=True)
    def _cleanup_skills(self):
        from fusion_cowork.skills.registry import SkillRegistry
        yield
        SkillRegistry()._skills.clear()

    def test_register_builtin_skills(self):
        from fusion_cowork.skills.registry import SkillRegistry
        from fusion_cowork.skills.builtin import register_builtin_skills
        SkillRegistry()._skills.clear()
        registry = SkillRegistry()
        register_builtin_skills(registry)
        skills = registry.list_skills()
        assert len(skills) >= 6

    def test_builtin_skill_names(self):
        from fusion_cowork.skills.registry import SkillRegistry
        from fusion_cowork.skills.builtin import register_builtin_skills, BUILTIN_SKILLS
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
