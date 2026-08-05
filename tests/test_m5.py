"""M5 里程碑测试 — Benchmark + 端到端 + Agent 真实执行 + Hook 集成 + SDK/Headless。"""

import asyncio
import json
import os
import tempfile
from unittest.mock import AsyncMock, MagicMock

import pytest

from fusion_cowork.benchmark.matrix import Capability, CapabilityLevel, CapabilityMatrix
from fusion_cowork.benchmark.report import ReportRenderer
from fusion_cowork.benchmark.runner import BenchmarkResult, BenchmarkRunner
from fusion_cowork.engine.events import EventEmitter
from fusion_cowork.engine.hooks import HookContext, HookEvent, HookManager
from fusion_cowork.engine.node import NodeConfig, NodeRegistry
from fusion_cowork.engine.permission import PermissionLevel, PermissionManager
from fusion_cowork.engine.session import Session, SessionStore
from fusion_cowork.engine.workflow import Workflow, WorkflowEngine, WorkflowStatus
from fusion_cowork.orchestrator.agent_runtime import AgentRuntime
from fusion_cowork.orchestrator.comm import AgentMessageBus
from fusion_cowork.orchestrator.executors import CoordinatorExecutor
from fusion_cowork.orchestrator.orchestrator import Agent, AgentOrchestrator, AgentRole, AgentTask
from fusion_cowork.server.desk_rpc import DeskRPCServer
from fusion_cowork.server.mcp_server import MCPToolRegistry

# ── CapabilityMatrix ──


class TestCapabilityMatrix:
    def test_default_caps_loaded(self):
        m = CapabilityMatrix()
        assert len(m.list_all()) == 32

    def test_categories(self):
        m = CapabilityMatrix()
        cats = m.categories()
        assert "桌面自动化" in cats
        assert "AI 能力" in cats
        assert "离线/隐私" in cats

    def test_by_category(self):
        m = CapabilityMatrix()
        ai = m.by_category("AI 能力")
        assert len(ai) == 4

    def test_parity_score(self):
        m = CapabilityMatrix()
        assert m.parity_score() > 1.0

    def test_desk_unique(self):
        m = CapabilityMatrix()
        assert m.desk_unique_count() >= 10
        assert m.cowork_unique_count() == 0

    def test_summary(self):
        m = CapabilityMatrix()
        s = m.summary()
        assert s["total_capabilities"] == 32
        assert s["desk_full_or_above"] > 0
        assert s["desk_unique"] >= 10

    def test_get_existing(self):
        m = CapabilityMatrix()
        cap = m.get("browser")
        assert cap is not None
        assert cap.desk_level == CapabilityLevel.FULL
        assert cap.cowork_level == CapabilityLevel.NONE

    def test_get_missing(self):
        m = CapabilityMatrix()
        assert m.get("nonexistent") is None

    def test_add_custom(self):
        m = CapabilityMatrix()
        cap = Capability(id="custom", name="Custom", category="Test", desk_level=CapabilityLevel.FULL)
        m.add(cap)
        assert m.get("custom") is not None
        assert len(m.list_all()) == 33

    def test_to_dict(self):
        m = CapabilityMatrix()
        d = m.to_dict()
        assert "summary" in d
        assert "capabilities" in d
        assert "browser" in d["capabilities"]

    def test_to_json(self):
        m = CapabilityMatrix()
        j = m.to_json()
        parsed = json.loads(j)
        assert parsed["summary"]["total_capabilities"] == 32


# ── Capability ──


class TestCapability:
    def test_parity_desk_adv_cowork_full(self):
        c = Capability(
            id="t", name="T", category="X", desk_level=CapabilityLevel.ADVANCED, cowork_level=CapabilityLevel.FULL
        )
        assert c.parity == 1.5

    def test_parity_equal(self):
        c = Capability(
            id="t", name="T", category="X", desk_level=CapabilityLevel.FULL, cowork_level=CapabilityLevel.FULL
        )
        assert c.parity == 1.0

    def test_parity_cowork_none_desk_has(self):
        c = Capability(
            id="t", name="T", category="X", desk_level=CapabilityLevel.FULL, cowork_level=CapabilityLevel.NONE
        )
        assert c.parity == 1.0

    def test_parity_both_none(self):
        c = Capability(
            id="t", name="T", category="X", desk_level=CapabilityLevel.NONE, cowork_level=CapabilityLevel.NONE
        )
        assert c.parity == 0.0

    def test_to_dict(self):
        c = Capability(
            id="t",
            name="T",
            category="X",
            desk_level=CapabilityLevel.FULL,
            cowork_level=CapabilityLevel.PARTIAL,
            desk_detail="dd",
            cowork_detail="cd",
        )
        d = c.to_dict()
        assert d["desk_level"] == "FULL"
        assert d["cowork_level"] == "PARTIAL"
        assert d["parity"] == 1.5


# ── BenchmarkRunner ──


class TestBenchmarkRunner:
    @pytest.mark.asyncio
    async def test_run_node(self):
        runner = BenchmarkRunner(warmup=0, repeats=1)
        result = await runner.run_node("file_input", {"path": "~"})
        assert result.status == "success"
        assert result.latency_ms > 0

    @pytest.mark.asyncio
    async def test_run_node_not_found(self):
        runner = BenchmarkRunner()
        result = await runner.run_node("nonexistent_node", {})
        assert result.status == "not_found"
        assert result.latency_ms == 0

    @pytest.mark.asyncio
    async def test_run_nodes_batch(self):
        runner = BenchmarkRunner(warmup=0, repeats=2)
        results = await runner.run_nodes(
            [
                {"node": "file_input", "params": {"path": "~"}},
            ]
        )
        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_summary(self):
        runner = BenchmarkRunner(warmup=0, repeats=2)
        await runner.run_node("file_input", {"path": "~"})
        await runner.run_node("file_input", {"path": "~"})
        s = runner.summary()
        assert "total_runs" in s
        assert s["total_runs"] == 2
        assert "file_input" in s["nodes"]

    def test_results_empty(self):
        runner = BenchmarkRunner()
        assert runner.results() == []
        assert runner.summary() == {"total": 0}

    @pytest.mark.asyncio
    async def test_to_json(self):
        runner = BenchmarkRunner(warmup=0, repeats=1)
        await runner.run_node("file_input", {"path": "~"})
        j = runner.to_json()
        parsed = json.loads(j)
        assert "summary" in parsed
        assert "results" in parsed


# ── ReportRenderer ──


class TestReportRenderer:
    def test_markdown_has_title(self):
        r = ReportRenderer(CapabilityMatrix())
        md = r.render_markdown()
        assert "Claude Cowork vs Fusion-Cowork" in md
        assert "总览" in md

    def test_markdown_has_categories(self):
        r = ReportRenderer(CapabilityMatrix())
        md = r.render_markdown()
        assert "桌面自动化" in md
        assert "AI 能力" in md

    def test_markdown_has_desk_unique(self):
        r = ReportRenderer(CapabilityMatrix())
        md = r.render_markdown()
        assert "Fusion-Cowork 独有优势" in md

    def test_markdown_with_benchmark(self):
        runner = BenchmarkRunner(warmup=0, repeats=1)
        runner._results.append(BenchmarkResult(node_name="test", status="success", latency_ms=42.5))
        r = ReportRenderer(CapabilityMatrix(), runner=runner)
        md = r.render_markdown()
        assert "性能基准" in md
        assert "42.5" in md

    def test_html_report(self):
        r = ReportRenderer(CapabilityMatrix())
        html = r.render_html()
        assert "<!DOCTYPE html>" in html
        assert "<table>" in html

    def test_save_markdown(self):
        r = ReportRenderer(CapabilityMatrix())
        with tempfile.NamedTemporaryFile(suffix=".md", delete=False, mode="w") as f:
            path = f.name
        try:
            r.save(path, fmt="markdown")
            with open(path) as f:
                assert "Claude Cowork vs Fusion-Cowork" in f.read()
        finally:
            os.unlink(path)

    def test_save_html(self):
        r = ReportRenderer(CapabilityMatrix())
        with tempfile.NamedTemporaryFile(suffix=".html", delete=False, mode="w") as f:
            path = f.name
        try:
            r.save(path, fmt="html")
            with open(path) as f:
                assert "<!DOCTYPE html>" in f.read()
        finally:
            os.unlink(path)


# ── 端到端: MCP 全链路 ──


class TestE2EMCP:
    @pytest.mark.asyncio
    async def test_mcp_tools_list(self):
        reg = MCPToolRegistry()
        reg.register_tools()
        tools = reg.list_tools()
        assert len(tools) == 16

    @pytest.mark.asyncio
    async def test_mcp_call_with_permission(self):
        pm = PermissionManager(level=PermissionLevel.BYPASS)
        hm = HookManager()
        reg = MCPToolRegistry(permission_manager=pm, hook_manager=hm)
        reg.register_tools()
        result = await reg.call_tool("read_file", {"path": "~"})
        assert "content" in result

    @pytest.mark.asyncio
    async def test_mcp_call_permission_denied(self):
        pm = PermissionManager(level=PermissionLevel.MANUAL)
        reg = MCPToolRegistry(permission_manager=pm)
        reg.register_tools()
        result = await reg.call_tool("run_terminal", {"command": "rm -rf /"})
        assert result.get("isError") or "denied" in json.dumps(result)


# ── 端到端: DeskRPC + Engine + Session + Event ──


class TestE2EDeskRPC:
    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = SessionStore(db_path=self.tmp.name)
        self.em = EventEmitter()
        self.pm = PermissionManager(level=PermissionLevel.BYPASS)
        self.hm = HookManager()
        self.rpc = DeskRPCServer(
            event_emitter=self.em,
            session_store=self.store,
            permission_manager=self.pm,
            hook_manager=self.hm,
        )

    def teardown_method(self):
        os.unlink(self.tmp.name)

    @pytest.mark.asyncio
    async def test_health(self):
        r = await self.rpc._dispatch({"jsonrpc": "2.0", "id": 1, "method": "desk.health", "params": {}})
        assert r["result"]["status"] == "ok"

    @pytest.mark.asyncio
    async def test_nodes_list(self):
        r = await self.rpc._dispatch({"jsonrpc": "2.0", "id": 2, "method": "desk.nodes.list", "params": {}})
        assert r["result"]["count"] > 0

    @pytest.mark.asyncio
    async def test_events_subscribe_and_recent(self):
        r1 = await self.rpc._dispatch({"jsonrpc": "2.0", "id": 3, "method": "desk.events.subscribe", "params": {}})
        assert "sub_id" in r1["result"]
        self.em.create_event("workflow_start", execution_id="e2e")
        r2 = await self.rpc._dispatch({"jsonrpc": "2.0", "id": 4, "method": "desk.events.recent", "params": {}})
        assert r2["result"]["count"] == 1

    @pytest.mark.asyncio
    async def test_session_workflow(self):
        s = Session(workflow_name="e2e_test")
        self.store.save(s)
        r1 = await self.rpc._dispatch({"jsonrpc": "2.0", "id": 5, "method": "desk.session.list", "params": {}})
        assert r1["result"]["count"] == 1
        r2 = await self.rpc._dispatch(
            {"jsonrpc": "2.0", "id": 6, "method": "desk.session.get", "params": {"session_id": s.id}}
        )
        assert r2["result"]["workflow_name"] == "e2e_test"

    @pytest.mark.asyncio
    async def test_permission_workflow(self):
        r1 = await self.rpc._dispatch(
            {"jsonrpc": "2.0", "id": 7, "method": "desk.permission.check", "params": {"tool_name": "shell_exec"}}
        )
        assert r1["result"]["allowed"] is True
        r2 = await self.rpc._dispatch({"jsonrpc": "2.0", "id": 8, "method": "desk.permission.list", "params": {}})
        assert "level" in r2["result"]

    @pytest.mark.asyncio
    async def test_dispatch_unknown_method(self):
        r = await self.rpc._dispatch({"jsonrpc": "2.0", "id": 9, "method": "desk.nonexistent", "params": {}})
        assert "error" in r
        assert r["error"]["code"] == -32601


# ── 端到端: WorkflowEngine + Permission + Hook + Session + Event ──


class TestE2EWorkflowFull:
    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = SessionStore(db_path=self.tmp.name)
        self.em = EventEmitter()
        self.pm = PermissionManager(level=PermissionLevel.BYPASS)
        self.hm = HookManager()

    def teardown_method(self):
        os.unlink(self.tmp.name)

    @pytest.mark.asyncio
    async def test_workflow_with_all_middleware(self):
        node = NodeRegistry.create("file_input", config=NodeConfig(params={"path": "~"}))
        wf = Workflow(workflow_id="e2e_wf", name="e2e_test")
        wf.add_node(node)
        engine = WorkflowEngine(
            permission_manager=self.pm,
            hook_manager=self.hm,
            session_store=self.store,
            event_emitter=self.em,
        )
        result = await engine.execute(wf)
        assert result.status == WorkflowStatus.SUCCESS
        assert len(self.em.get_buffered()) >= 2
        sessions = self.store.list_sessions()
        assert len(sessions) >= 1

    @pytest.mark.asyncio
    async def test_workflow_permission_denied(self):
        pm_deny = PermissionManager(level=PermissionLevel.MANUAL)
        node = NodeRegistry.create("shell_exec", config=NodeConfig(params={"command": "echo hi"}))
        wf = Workflow(workflow_id="e2e_deny", name="deny_test")
        wf.add_node(node)
        engine = WorkflowEngine(permission_manager=pm_deny, hook_manager=self.hm)
        result = await engine.execute(wf)
        assert result.status in (WorkflowStatus.FAILED, WorkflowStatus.SUCCESS)

    @pytest.mark.asyncio
    async def test_workflow_hook_cancel(self):
        async def cancel_hook(ctx: HookContext):
            ctx.cancel()

        self.hm.register(HookEvent.PRE_NODE_EXECUTE, cancel_hook)
        node = NodeRegistry.create("file_input", config=NodeConfig(params={"path": "~"}))
        wf = Workflow(workflow_id="e2e_cancel", name="cancel_test")
        wf.add_node(node)
        engine = WorkflowEngine(hook_manager=self.hm)
        result = await engine.execute(wf)
        assert result.status in (WorkflowStatus.CANCELLED, WorkflowStatus.SUCCESS)


# ── CLI Benchmark 命令 ──


class TestCLIBenchmark:
    def test_benchmark_report_markdown(self):
        from click.testing import CliRunner

        from fusion_cowork.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["benchmark", "report", "--format", "markdown"])
        assert result.exit_code == 0
        assert "Claude Cowork vs Fusion-Cowork" in result.output

    def test_benchmark_report_json(self):
        from click.testing import CliRunner

        from fusion_cowork.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["benchmark", "report", "--format", "json"])
        assert result.exit_code == 0
        json_start = result.output.index("{")
        parsed = json.loads(result.output[json_start:])
        assert parsed["summary"]["total_capabilities"] == 32

    def test_benchmark_report_save(self):
        from click.testing import CliRunner

        from fusion_cowork.cli import cli

        runner = CliRunner()
        with tempfile.NamedTemporaryFile(suffix=".md", delete=False) as f:
            path = f.name
        try:
            result = runner.invoke(cli, ["benchmark", "report", "--format", "markdown", "-o", path])
            assert result.exit_code == 0
            with open(path) as f:
                assert "Claude Cowork vs Fusion-Cowork" in f.read()
        finally:
            os.unlink(path)


# ── W13: Agent 真实执行层 ──


class TestAgentRuntime:
    def test_runtime_creation(self):
        agent = Agent(agent_id="test", name="Test", role=AgentRole.EXECUTOR)
        bus = AgentMessageBus()
        executor = AsyncMock(return_value={"status": "ok"})
        rt = AgentRuntime(agent, executor, bus)
        assert rt.agent.agent_id == "test"
        assert not rt.is_running

    @pytest.mark.asyncio
    async def test_runtime_start_stop(self):
        agent = Agent(agent_id="rt_test", name="RT Test", role=AgentRole.EXECUTOR)
        bus = AgentMessageBus()
        executor = AsyncMock(return_value={"status": "ok"})
        rt = AgentRuntime(agent, executor, bus)
        await rt.start()
        assert rt.is_running
        await rt.stop()
        assert not rt.is_running

    @pytest.mark.asyncio
    async def test_runtime_submit_and_result(self):
        agent = Agent(agent_id="exec_test", name="Exec Test", role=AgentRole.EXECUTOR)
        bus = AgentMessageBus()
        executor = AsyncMock(return_value={"result": "done"})
        rt = AgentRuntime(agent, executor, bus)
        await rt.start()
        await rt.submit("task_001", {"prompt": "hello"})
        await asyncio.sleep(1.0)
        result = rt.get_result("task_001")
        assert result is not None
        assert result["result"] == "done"
        await rt.stop()

    @pytest.mark.asyncio
    async def test_runtime_error_handling(self):
        agent = Agent(agent_id="err_test", name="Err Test", role=AgentRole.EXECUTOR)
        bus = AgentMessageBus()
        executor = AsyncMock(side_effect=ValueError("boom"))
        rt = AgentRuntime(agent, executor, bus)
        await rt.start()
        await rt.submit("task_err", {"prompt": "fail"})
        await asyncio.sleep(1.0)
        result = rt.get_result("task_err")
        assert result is not None
        assert "error" in result
        await rt.stop()


class TestCoordinatorExecutor:
    @pytest.mark.asyncio
    async def test_coordinator_no_orchestrator(self):
        coord = CoordinatorExecutor()
        result = await coord({"prompt": "test"})
        assert "error" in result

    @pytest.mark.asyncio
    async def test_coordinator_no_prompt(self):
        orch = MagicMock()
        coord = CoordinatorExecutor(orch)
        result = await coord({})
        assert "error" in result

    @pytest.mark.asyncio
    async def test_coordinator_dispatches_task(self):
        orch = MagicMock()
        task = AgentTask(
            task_id="task_001",
            agent_id="executor_node",
            description="test",
            status="completed",
            output_data={"status": "ok"},
        )
        orch.submit_task = AsyncMock(return_value="task_001")
        orch.get_task = MagicMock(return_value=task)
        coord = CoordinatorExecutor(orch)
        result = await coord({"prompt": "do something", "subtask_type": "node"})
        assert result["status"] == "ok"


class TestAgentOrchestratorEnhanced:
    def test_register_default_agents_with_coordinator(self):
        orch = AgentOrchestrator()
        orch.register_default_agents()
        assert "coordinator" in orch._agents
        assert "coordinator" in orch._executors

    @pytest.mark.asyncio
    async def test_start_stop_runtimes(self):
        orch = AgentOrchestrator()
        orch.register_default_agents()
        await orch.start_runtimes()
        assert len(orch._runtimes) > 0
        await orch.stop_runtimes()
        assert len(orch._runtimes) == 0

    def test_message_bus_created(self):
        orch = AgentOrchestrator()
        orch.register_default_agents()
        assert orch._message_bus is not None

    def test_get_message_bus(self):
        orch = AgentOrchestrator()
        orch.register_default_agents()
        bus = orch.get_message_bus()
        assert bus is not None


# ── W14: Hook 生命周期集成 ──


class TestHookNewEvents:
    def test_session_start_event(self):
        assert hasattr(HookEvent, "SESSION_START")
        assert HookEvent.SESSION_START.value == "session_start"

    def test_session_end_event(self):
        assert hasattr(HookEvent, "SESSION_END")
        assert HookEvent.SESSION_END.value == "session_end"

    def test_pre_compact_event(self):
        assert hasattr(HookEvent, "PRE_COMPACT")
        assert HookEvent.PRE_COMPACT.value == "pre_compact"


class TestHookPriority:
    @pytest.mark.asyncio
    async def test_register_with_priority(self):
        mgr = HookManager()
        calls = []

        def h1(ctx):
            calls.append("h1")

        def h2(ctx):
            calls.append("h2")

        mgr.register(HookEvent.PRE_NODE_EXECUTE, h2, priority=1)
        mgr.register(HookEvent.PRE_NODE_EXECUTE, h1, priority=10)
        await mgr.fire(HookEvent.PRE_NODE_EXECUTE, {"test": True})
        assert "h1" in calls
        assert "h2" in calls

    @pytest.mark.asyncio
    async def test_hook_fires_session_events(self):
        mgr = HookManager()
        received = []
        mgr.register(HookEvent.SESSION_START, lambda ctx: received.append("session_start"))
        mgr.register(HookEvent.PRE_COMPACT, lambda ctx: received.append("pre_compact"))
        await mgr.fire(HookEvent.SESSION_START, {"session_id": "s1"})
        await mgr.fire(HookEvent.PRE_COMPACT, {"token_count": 5000})
        assert "session_start" in received
        assert "pre_compact" in received


class TestHookWorkflowIntegration:
    @pytest.mark.asyncio
    async def test_workflow_fire_hooks(self):
        from fusion_cowork.nodes.macos import DesktopCleanNode

        mgr = HookManager()
        events = []
        mgr.register(HookEvent.WORKFLOW_START, lambda ctx: events.append("start"))
        mgr.register(HookEvent.WORKFLOW_END, lambda ctx: events.append("end"))
        mgr.register(HookEvent.PRE_NODE_EXECUTE, lambda ctx: events.append("pre_node"))
        mgr.register(HookEvent.POST_NODE_EXECUTE, lambda ctx: events.append("post_node"))
        engine = WorkflowEngine(hook_manager=mgr)
        wf = Workflow(name="hook_test")
        wf.add_node(DesktopCleanNode())
        _result = await engine.execute(wf)
        assert "start" in events
        assert "end" in events

    @pytest.mark.asyncio
    async def test_hook_cancel_node(self):
        from fusion_cowork.nodes.macos import DesktopCleanNode

        mgr = HookManager()

        def cancel_hook(ctx: HookContext):
            ctx.cancel()

        mgr.register(HookEvent.PRE_NODE_EXECUTE, cancel_hook)
        engine = WorkflowEngine(hook_manager=mgr)
        wf = Workflow(name="cancel_test")
        wf.add_node(DesktopCleanNode())
        result = await engine.execute(wf)
        assert result.status == WorkflowStatus.CANCELLED

    @pytest.mark.asyncio
    async def test_hook_modify_params(self):
        from fusion_cowork.nodes.macos import DesktopCleanNode

        mgr = HookManager()
        mod_events = []

        def modify_hook(ctx: HookContext):
            if ctx.event == HookEvent.PRE_NODE_EXECUTE:
                ctx.modify("params_overridden", True)
                mod_events.append("modified")

        mgr.register(HookEvent.PRE_NODE_EXECUTE, modify_hook)
        engine = WorkflowEngine(hook_manager=mgr)
        wf = Workflow(name="modify_test")
        wf.add_node(DesktopCleanNode())
        _result = await engine.execute(wf)
        assert "modified" in mod_events

    @pytest.mark.asyncio
    async def test_hook_node_error(self):
        from fusion_cowork.nodes.macos import DesktopCleanNode

        mgr = HookManager()
        error_events = []
        mgr.register(HookEvent.NODE_ERROR, lambda ctx: error_events.append("node_error"))
        engine = WorkflowEngine(hook_manager=mgr)
        wf = Workflow(name="error_test")
        node = DesktopCleanNode(node_id="will_fail")
        node.execute = AsyncMock(side_effect=RuntimeError("forced error"))
        wf.add_node(node)
        _result = await engine.execute(wf)
        assert "node_error" in error_events


class TestHookPermissionIntegration:
    @pytest.mark.asyncio
    async def test_permission_hook_auto_approve(self):
        mgr = HookManager()

        def auto_approve(ctx: HookContext):
            ctx.modify("approved", True)

        mgr.register(HookEvent.PERMISSION_REQUEST, auto_approve)
        pm = PermissionManager(level=PermissionLevel.MANUAL, hook_manager=mgr)
        result = await pm.check("shell_exec", "execute", {"command": "ls"})
        assert result is True

    @pytest.mark.asyncio
    async def test_permission_hook_auto_deny(self):
        mgr = HookManager()

        def auto_deny(ctx: HookContext):
            ctx.cancel()

        mgr.register(HookEvent.PERMISSION_REQUEST, auto_deny)
        pm = PermissionManager(level=PermissionLevel.AUTO, hook_manager=mgr)
        result = await pm.check("shell_exec", "execute", {"command": "rm -rf /"})
        assert result is False


# ── W15: SDK/Headless ──


class TestHeadlessRunner:
    @pytest.mark.asyncio
    async def test_headless_runner_run_template(self):
        from fusion_cowork.sdk.headless import HeadlessRunner

        runner = HeadlessRunner()
        result = await runner.run_template("desktop_clean")
        assert result is not None

    @pytest.mark.asyncio
    async def test_headless_runner_run_workflow(self):
        from fusion_cowork.nodes.macos import DesktopCleanNode  # noqa: F401 — trigger registration
        from fusion_cowork.sdk.headless import HeadlessRunner

        runner = HeadlessRunner()
        wf_def = {
            "name": "test_wf",
            "nodes": [{"id": "n1", "name": "desktop_clean"}],
            "edges": [],
        }
        result = await runner.run_workflow(wf_def)
        assert result is not None

    @pytest.mark.asyncio
    async def test_headless_runner_cancel(self):
        from fusion_cowork.sdk.headless import HeadlessRunner

        runner = HeadlessRunner()
        runner._running = True
        await runner.cancel()
        assert not runner._running


class TestFusionCoworkSDK:
    @pytest.mark.asyncio
    async def test_sdk_list_nodes(self):
        from fusion_cowork.sdk import FusionCoworkSDK

        sdk = FusionCoworkSDK(base_url="http://localhost:19999")
        nodes = await sdk.list_nodes()
        assert isinstance(nodes, list)
        assert len(nodes) > 0

    @pytest.mark.asyncio
    async def test_sdk_list_templates(self):
        from fusion_cowork.sdk import FusionCoworkSDK

        sdk = FusionCoworkSDK(base_url="http://localhost:19999")
        templates = await sdk.list_templates()
        assert isinstance(templates, list)


class TestSDKModuleImport:
    def test_sdk_import(self):
        from fusion_cowork.sdk import FusionCoworkSDK

        assert FusionCoworkSDK is not None

    def test_headless_import(self):
        from fusion_cowork.sdk.headless import HeadlessRunner

        assert HeadlessRunner is not None

    def test_lazy_import_agent_runtime(self):
        from fusion_cowork import AgentRuntime

        assert AgentRuntime is not None

    def test_lazy_import_coordinator_executor(self):
        from fusion_cowork import CoordinatorExecutor

        assert CoordinatorExecutor is not None
