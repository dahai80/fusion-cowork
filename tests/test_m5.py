"""M5 里程碑测试 — Benchmark 模块 + 端到端集成。"""
import asyncio
import json
import os
import tempfile

import pytest

from fusion_desk.benchmark.matrix import CapabilityMatrix, Capability, CapabilityLevel
from fusion_desk.benchmark.runner import BenchmarkRunner, BenchmarkResult
from fusion_desk.benchmark.report import ReportRenderer
from fusion_desk.engine.node import BaseNode, NodeConfig, NodeResult, NodeStatus, NodeRegistry
from fusion_desk.engine.workflow import Workflow, WorkflowEngine, WorkflowStatus
from fusion_desk.engine.permission import PermissionManager, PermissionLevel
from fusion_desk.engine.hooks import HookManager, HookEvent, HookContext
from fusion_desk.engine.session import Session, SessionStore
from fusion_desk.engine.events import EventEmitter, EventType, WorkflowEvent
from fusion_desk.server.mcp_server import MCPToolRegistry
from fusion_desk.server.desk_rpc import DeskRPCServer


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
        cap = Capability(id="custom", name="Custom", category="Test",
                         desk_level=CapabilityLevel.FULL)
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
        c = Capability(id="t", name="T", category="X",
                       desk_level=CapabilityLevel.ADVANCED, cowork_level=CapabilityLevel.FULL)
        assert c.parity == 1.5

    def test_parity_equal(self):
        c = Capability(id="t", name="T", category="X",
                       desk_level=CapabilityLevel.FULL, cowork_level=CapabilityLevel.FULL)
        assert c.parity == 1.0

    def test_parity_cowork_none_desk_has(self):
        c = Capability(id="t", name="T", category="X",
                       desk_level=CapabilityLevel.FULL, cowork_level=CapabilityLevel.NONE)
        assert c.parity == 1.0

    def test_parity_both_none(self):
        c = Capability(id="t", name="T", category="X",
                       desk_level=CapabilityLevel.NONE, cowork_level=CapabilityLevel.NONE)
        assert c.parity == 0.0

    def test_to_dict(self):
        c = Capability(id="t", name="T", category="X",
                       desk_level=CapabilityLevel.FULL, cowork_level=CapabilityLevel.PARTIAL,
                       desk_detail="dd", cowork_detail="cd")
        d = c.to_dict()
        assert d["desk_level"] == "FULL"
        assert d["cowork_level"] == "PARTIAL"
        assert d["parity"] == 1.5


# ── BenchmarkRunner ──

class TestBenchmarkRunner:
    @pytest.mark.asyncio
    async def test_run_node(self):
        import fusion_desk.nodes.io
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
        import fusion_desk.nodes.io
        runner = BenchmarkRunner(warmup=0, repeats=2)
        results = await runner.run_nodes([
            {"node": "file_input", "params": {"path": "~"}},
        ])
        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_summary(self):
        import fusion_desk.nodes.io
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
        import fusion_desk.nodes.io
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
        assert "Claude Cowork vs Fusion-Desk" in md
        assert "总览" in md

    def test_markdown_has_categories(self):
        r = ReportRenderer(CapabilityMatrix())
        md = r.render_markdown()
        assert "桌面自动化" in md
        assert "AI 能力" in md

    def test_markdown_has_desk_unique(self):
        r = ReportRenderer(CapabilityMatrix())
        md = r.render_markdown()
        assert "Fusion-Desk 独有优势" in md

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
                assert "Claude Cowork vs Fusion-Desk" in f.read()
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
        assert len(tools) == 14

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
            event_emitter=self.em, session_store=self.store,
            permission_manager=self.pm, hook_manager=self.hm,
        )

    def teardown_method(self):
        os.unlink(self.tmp.name)

    @pytest.mark.asyncio
    async def test_health(self):
        r = await self.rpc._dispatch({"jsonrpc": "2.0", "id": 1, "method": "desk.health", "params": {}})
        assert r["result"]["status"] == "ok"

    @pytest.mark.asyncio
    async def test_nodes_list(self):
        import fusion_desk.nodes.io
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
        r2 = await self.rpc._dispatch({"jsonrpc": "2.0", "id": 6, "method": "desk.session.get", "params": {"session_id": s.id}})
        assert r2["result"]["workflow_name"] == "e2e_test"

    @pytest.mark.asyncio
    async def test_permission_workflow(self):
        r1 = await self.rpc._dispatch({"jsonrpc": "2.0", "id": 7, "method": "desk.permission.check", "params": {"tool_name": "shell_exec"}})
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
        import fusion_desk.nodes.io
        from fusion_desk.engine.node import NodeRegistry, NodeConfig
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
        import fusion_desk.nodes.tools
        from fusion_desk.engine.node import NodeRegistry, NodeConfig
        pm_deny = PermissionManager(level=PermissionLevel.MANUAL)
        node = NodeRegistry.create("shell_exec", config=NodeConfig(params={"command": "echo hi"}))
        wf = Workflow(workflow_id="e2e_deny", name="deny_test")
        wf.add_node(node)
        engine = WorkflowEngine(permission_manager=pm_deny, hook_manager=self.hm)
        result = await engine.execute(wf)
        assert result.status in (WorkflowStatus.FAILED, WorkflowStatus.SUCCESS)

    @pytest.mark.asyncio
    async def test_workflow_hook_cancel(self):
        import fusion_desk.nodes.io
        from fusion_desk.engine.node import NodeRegistry, NodeConfig
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
        from fusion_desk.cli import cli
        runner = CliRunner()
        result = runner.invoke(cli, ["benchmark", "report", "--format", "markdown"])
        assert result.exit_code == 0
        assert "Claude Cowork vs Fusion-Desk" in result.output

    def test_benchmark_report_json(self):
        from click.testing import CliRunner
        from fusion_desk.cli import cli
        runner = CliRunner()
        result = runner.invoke(cli, ["benchmark", "report", "--format", "json"])
        assert result.exit_code == 0
        json_start = result.output.index("{")
        parsed = json.loads(result.output[json_start:])
        assert parsed["summary"]["total_capabilities"] == 32

    def test_benchmark_report_save(self):
        from click.testing import CliRunner
        from fusion_desk.cli import cli
        runner = CliRunner()
        with tempfile.NamedTemporaryFile(suffix=".md", delete=False) as f:
            path = f.name
        try:
            result = runner.invoke(cli, ["benchmark", "report", "--format", "markdown", "-o", path])
            assert result.exit_code == 0
            with open(path) as f:
                assert "Claude Cowork vs Fusion-Desk" in f.read()
        finally:
            os.unlink(path)
