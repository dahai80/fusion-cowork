"""Fusion-Cowork core engine unit tests."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

import fusion_cowork.nodes.ai
import fusion_cowork.nodes.io
import fusion_cowork.nodes.logic

# Import all node modules so they get registered
import fusion_cowork.nodes.macos
import fusion_cowork.nodes.tools  # noqa: F401
from fusion_cowork.engine.node import (
    BaseNode,
    NodeCategory,
    NodeConfig,
    NodeRegistry,
    NodeResult,
    NodeStatus,
    _coerce_array,
    _coerce_bool,
    _coerce_int,
    _coerce_number,
    coerce_param,
    coerce_params,
    register_node,
)
from fusion_cowork.engine.workflow import Edge, Workflow, WorkflowEngine, WorkflowStatus

# ── Test mock nodes ──


@register_node
class MockSuccessNode(BaseNode):
    name = "mock_success"
    display_name = "Mock 成功节点"  # i18n: user-facing
    category = NodeCategory.IO
    description = "测试用：总是成功"  # i18n: user-facing
    icon = "🧪"
    default_label = "Mock 成功"  # i18n: user-facing

    async def execute(self, inputs: dict) -> NodeResult:
        return NodeResult(
            status=NodeStatus.SUCCESS,
            data={"result": "ok", "input": inputs},
            summary="Mock 执行成功",  # i18n: user-facing
        )


@register_node
class MockFailNode(BaseNode):
    name = "mock_fail"
    display_name = "Mock 失败节点"  # i18n: user-facing
    category = NodeCategory.IO
    description = "测试用：总是失败"  # i18n: user-facing
    icon = "🧪"
    default_label = "Mock 失败"  # i18n: user-facing

    async def execute(self, inputs: dict) -> NodeResult:
        return NodeResult(
            status=NodeStatus.FAILED,
            error="Mock 执行失败",  # i18n: user-facing
            summary="Mock 执行失败",  # i18n: user-facing
        )


@register_node
class MockTransformNode(BaseNode):
    """Test node: transforms input data."""

    name = "mock_transform"
    display_name = "Mock 转换"  # i18n: user-facing
    category = NodeCategory.IO
    description = "测试用：数据转换"  # i18n: user-facing
    icon = "🧪"
    default_label = "Mock 转换"  # i18n: user-facing

    def get_params_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "prefix": {"type": "string", "default": "transformed_"},
            },
        }

    async def execute(self, inputs: dict) -> NodeResult:
        prefix = self.config.params.get("prefix", "transformed_")
        return NodeResult(
            status=NodeStatus.SUCCESS,
            data={"transformed": f"{prefix}{json.dumps(inputs)}"},
            summary="转换完成",  # i18n: user-facing
        )


# ── NodeRegistry tests ──


class TestNodeRegistry:
    def test_register_and_get(self):
        cls = NodeRegistry.get("mock_success")
        assert cls is not None
        assert cls.name == "mock_success"

    def test_create_instance(self):
        node = NodeRegistry.create("mock_success")
        assert node is not None
        assert node.name == "mock_success"
        assert isinstance(node, BaseNode)

    def test_create_with_config(self):
        config = NodeConfig(label="测试节点", params={"key": "value"})  # i18n: user-facing
        node = NodeRegistry.create("mock_success", config=config)
        assert node is not None
        assert node.config.label == "测试节点"  # i18n: user-facing
        assert node.config.params["key"] == "value"

    def test_create_unknown(self):
        node = NodeRegistry.create("non_existent_node")
        assert node is None

    def test_list(self):
        nodes = NodeRegistry.list()
        names = [n["name"] for n in nodes]
        assert "mock_success" in names
        assert "mock_fail" in names

    def test_list_by_category(self):
        nodes = NodeRegistry.list(category=NodeCategory.IO)
        for n in nodes:
            assert n["category"] == "io"

    def test_register_decorator(self):
        # register_node decorator already invoked at class definition time
        assert NodeRegistry.get("mock_success") is not None

    def test_clear(self):
        # Save registry state to avoid breaking other tests
        saved_registry = dict(NodeRegistry._registry)
        NodeRegistry.clear()
        assert NodeRegistry.get("mock_success") is None
        # Restore registry
        NodeRegistry._registry.clear()
        NodeRegistry._registry.update(saved_registry)


# ── BaseNode tests ──


class TestBaseNode:
    def test_node_initialization(self):
        node = MockSuccessNode()
        assert node.id.startswith("node_")
        assert node.status == NodeStatus.PENDING
        assert node.result is None

    def test_node_with_id(self):
        node = MockSuccessNode(node_id="test_123")
        assert node.id == "test_123"

    def test_node_with_config(self):
        config = NodeConfig(label="测试", params={"key": "value"})  # i18n: user-facing
        node = MockSuccessNode(config=config)
        assert node.config.label == "测试"  # i18n: user-facing
        assert node.config.params["key"] == "value"

    def test_validate_config_default(self):
        node = MockSuccessNode()
        errors = node.validate_config()
        assert errors == []

    def test_to_dict(self):
        node = MockSuccessNode(node_id="test_id", config=NodeConfig(label="测试"))  # i18n: user-facing
        d = node.to_dict()
        assert d["id"] == "test_id"
        assert d["name"] == "mock_success"
        assert d["config"]["label"] == "测试"  # i18n: user-facing

    def test_from_dict(self):
        data = {
            "id": "test_id",
            "name": "mock_success",
            "config": {
                "label": "测试节点",  # i18n: user-facing
                "params": {"key": "value"},
            },
        }
        node = MockSuccessNode.from_dict(data)
        assert node.id == "test_id"
        assert node.config.label == "测试节点"  # i18n: user-facing
        assert node.config.params["key"] == "value"

    @pytest.mark.asyncio
    async def test_execute_success(self):
        node = MockSuccessNode()
        result = await node.execute({"input": "test"})
        assert result.status == NodeStatus.SUCCESS
        assert result.data["result"] == "ok"
        assert result.summary == "Mock 执行成功"  # i18n: user-facing

    @pytest.mark.asyncio
    async def test_execute_fail(self):
        node = MockFailNode()
        result = await node.execute({})
        assert result.status == NodeStatus.FAILED
        assert result.error == "Mock 执行失败"  # i18n: user-facing


# ── Workflow tests ──


class TestWorkflow:
    def test_create_workflow(self):
        wf = Workflow(name="测试工作流", description="测试描述")  # i18n: user-facing
        assert wf.name == "测试工作流"  # i18n: user-facing
        assert wf.description == "测试描述"  # i18n: user-facing
        assert wf.id.startswith("wf_")
        assert len(wf.nodes) == 0
        assert len(wf.edges) == 0

    def test_add_node(self):
        wf = Workflow()
        node = MockSuccessNode(node_id="n1")
        nid = wf.add_node(node)
        assert nid == "n1"
        assert len(wf.nodes) == 1

    def test_remove_node(self):
        wf = Workflow()
        wf.add_node(MockSuccessNode(node_id="n1"))
        wf.add_node(MockSuccessNode(node_id="n2"))
        wf.connect("n1", "n2")
        assert wf.remove_node("n1")
        assert "n1" not in wf.nodes
        assert len(wf.edges) == 0

    def test_connect_nodes(self):
        wf = Workflow()
        wf.add_node(MockSuccessNode(node_id="n1"))
        wf.add_node(MockSuccessNode(node_id="n2"))
        assert wf.connect("n1", "n2")
        assert len(wf.edges) == 1
        assert wf.edges[0].source_id == "n1"
        assert wf.edges[0].target_id == "n2"

    def test_cycle_detection(self):
        wf = Workflow()
        wf.add_node(MockSuccessNode(node_id="n1"))
        wf.add_node(MockSuccessNode(node_id="n2"))
        wf.connect("n1", "n2")
        # Connecting in reverse would form a cycle
        assert not wf.connect("n2", "n1")

    def test_topological_sort(self):
        wf = Workflow()
        wf.add_node(MockSuccessNode(node_id="n1"))
        wf.add_node(MockSuccessNode(node_id="n2"))
        wf.add_node(MockSuccessNode(node_id="n3"))
        wf.connect("n1", "n2")
        wf.connect("n2", "n3")
        order = wf.topological_sort()
        assert order == ["n1", "n2", "n3"]

    def test_topological_sort_parallel(self):
        wf = Workflow()
        wf.add_node(MockSuccessNode(node_id="n1"))
        wf.add_node(MockSuccessNode(node_id="n2"))
        wf.add_node(MockSuccessNode(node_id="n3"))
        wf.connect("n1", "n3")
        wf.connect("n2", "n3")
        order = wf.topological_sort()
        # n1 and n2 order is not deterministic, but both must come before n3
        assert order[0] in ("n1", "n2")
        assert order[1] in ("n1", "n2")
        assert order[2] == "n3"

    def test_get_start_nodes(self):
        wf = Workflow()
        wf.add_node(MockSuccessNode(node_id="n1"))
        wf.add_node(MockSuccessNode(node_id="n2"))
        wf.connect("n1", "n2")
        start_nodes = wf.get_start_nodes()
        assert start_nodes == ["n1"]

    def test_get_upstream_downstream(self):
        wf = Workflow()
        wf.add_node(MockSuccessNode(node_id="n1"))
        wf.add_node(MockSuccessNode(node_id="n2"))
        wf.add_node(MockSuccessNode(node_id="n3"))
        wf.connect("n1", "n2")
        wf.connect("n2", "n3")
        assert wf.get_upstream_nodes("n2") == ["n1"]
        assert wf.get_downstream_nodes("n2") == ["n3"]

    def test_validate_empty(self):
        wf = Workflow()
        errors = wf.validate()
        assert len(errors) > 0
        assert any("没有节点" in e for e in errors)  # i18n: user-facing

    def test_validate_isolated_nodes(self):
        wf = Workflow()
        wf.add_node(MockSuccessNode(node_id="n1"))
        wf.add_node(MockSuccessNode(node_id="n2"))
        wf.connect("n1", "n2")
        errors = wf.validate()
        assert len(errors) == 0

    def test_serialization_roundtrip(self):
        wf = Workflow(name="测试", description="序列化测试")  # i18n: user-facing
        wf.add_node(MockSuccessNode(node_id="n1", config=NodeConfig(label="节点1")))  # i18n: user-facing
        wf.add_node(
            MockTransformNode(node_id="n2", config=NodeConfig(label="节点2", params={"prefix": "test_"}))  # i18n: user-facing
        )  # i18n: user-facing
        wf.connect("n1", "n2")

        # Serialize
        d = wf.to_dict()
        assert d["name"] == "测试"  # i18n: user-facing
        assert len(d["nodes"]) == 2
        assert len(d["edges"]) == 1

        # Deserialize
        wf2 = Workflow.from_dict(d)
        assert wf2.name == "测试"  # i18n: user-facing
        assert len(wf2.nodes) == 2
        assert len(wf2.edges) == 1
        assert wf2.nodes["n1"].name == "mock_success"
        assert wf2.nodes["n2"].config.params["prefix"] == "test_"

    def test_json_roundtrip(self):
        wf = Workflow(name="JSON测试")  # i18n: user-facing
        wf.add_node(MockSuccessNode(node_id="n1"))
        json_str = wf.to_json()
        wf2 = Workflow.from_json(json_str)
        assert wf2.name == "JSON测试"  # i18n: user-facing
        assert "n1" in wf2.nodes


# ── WorkflowEngine tests ──


class TestWorkflowEngine:
    @pytest.mark.asyncio
    async def test_execute_simple_workflow(self):
        wf = Workflow(name="简单工作流")  # i18n: user-facing
        wf.add_node(MockSuccessNode(node_id="n1"))
        wf.add_node(MockSuccessNode(node_id="n2"))
        wf.connect("n1", "n2")

        engine = WorkflowEngine()
        execution = await engine.execute(wf)

        assert execution.status == WorkflowStatus.SUCCESS
        assert len(execution.steps) == 2
        assert execution.steps[0].node_id == "n1"
        assert execution.steps[1].node_id == "n2"

    @pytest.mark.asyncio
    async def test_execute_with_failure(self):
        wf = Workflow(name="失败工作流")  # i18n: user-facing
        wf.add_node(MockSuccessNode(node_id="n1"))
        wf.add_node(MockFailNode(node_id="n2"))
        wf.connect("n1", "n2")

        engine = WorkflowEngine()
        execution = await engine.execute(wf)

        assert execution.status == WorkflowStatus.FAILED
        assert execution.error is not None
        assert "Mock 执行失败" in execution.error  # i18n: user-facing

    @pytest.mark.asyncio
    async def test_execute_continue_on_error(self):
        wf = Workflow(name="继续执行")  # i18n: user-facing
        cfg = NodeConfig(continue_on_error=True)
        wf.add_node(MockSuccessNode(node_id="n1"))
        wf.add_node(MockFailNode(node_id="n2", config=cfg))
        wf.add_node(MockSuccessNode(node_id="n3"))
        wf.connect("n1", "n2")
        wf.connect("n2", "n3")

        engine = WorkflowEngine()
        execution = await engine.execute(wf)

        # Even though n2 fails, n3 still executes because of continue_on_error
        assert execution.status == WorkflowStatus.SUCCESS
        assert len(execution.steps) == 3

    @pytest.mark.asyncio
    async def test_execute_empty_workflow(self):
        wf = Workflow(name="空工作流")  # i18n: user-facing
        engine = WorkflowEngine()
        execution = await engine.execute(wf)
        assert execution.status == WorkflowStatus.FAILED

    @pytest.mark.asyncio
    async def test_execute_with_data_passing(self):
        wf = Workflow(name="数据传递")  # i18n: user-facing
        wf.add_node(MockTransformNode(node_id="n1", config=NodeConfig(params={"prefix": "data_"})))
        wf.add_node(MockSuccessNode(node_id="n2"))
        wf.connect("n1", "n2")

        engine = WorkflowEngine()
        execution = await engine.execute(wf, initial_input={"key": "value"})

        assert execution.status == WorkflowStatus.SUCCESS
        # n2 should receive n1's output data
        n2_step = execution.steps[1]
        assert n2_step.node_id == "n2"

    @pytest.mark.asyncio
    async def test_execute_resume_skips_completed(self):
        # P1-2 resume: completed nodes in resume_steps skip re-run, downstream uses cached output
        wf = Workflow(name="断点续跑")  # i18n: user-facing
        wf.add_node(MockTransformNode(node_id="n1", config=NodeConfig(params={"prefix": "resume_"})))
        wf.add_node(MockSuccessNode(node_id="n2"))
        wf.connect("n1", "n2")

        # n1 snapshot: already succeeded, output_data is cached output (simulating prior execution result)
        resume_steps = [
            {
                "node_id": "n1",
                "node_name": "mock_transform",
                "node_display_name": "Mock 转换",  # i18n: user-facing
                "status": "success",
                "output_data": {"transformed": "resume_CACHED_OUTPUT"},
                "summary": "恢复自快照",  # i18n: user-facing
            }
        ]

        engine = WorkflowEngine()
        execution = await engine.execute(wf, resume_steps=resume_steps)

        assert execution.status == WorkflowStatus.SUCCESS
        assert len(execution.steps) == 2
        # n1 skipped, status SKIPPED
        assert execution.steps[0].node_id == "n1"
        assert execution.steps[0].status == NodeStatus.SKIPPED
        assert execution.steps[0].summary == "恢复自快照, 跳过重跑"  # i18n: user-facing
        # n2 actually executes, receiving n1's cached output as input
        assert execution.steps[1].node_id == "n2"
        assert execution.steps[1].status == NodeStatus.SUCCESS
        assert execution.steps[1].input_data.get("transformed") == "resume_CACHED_OUTPUT"

    @pytest.mark.asyncio
    async def test_execute_resume_partial_snapshot(self):
        # P1-2: resume_steps contains non-success steps, only success nodes are restored
        wf = Workflow(name="部分快照续跑")  # i18n: user-facing
        wf.add_node(MockSuccessNode(node_id="n1"))
        wf.add_node(MockSuccessNode(node_id="n2"))
        wf.connect("n1", "n2")

        resume_steps = [
            {"node_id": "n1", "status": "success", "output_data": {"result": "ok"}},
            {"node_id": "n2", "status": "failed", "output_data": {}},
        ]

        engine = WorkflowEngine()
        execution = await engine.execute(wf, resume_steps=resume_steps)

        assert execution.status == WorkflowStatus.SUCCESS
        # n1 skipped (success snapshot), n2 re-runs (failed snapshot does not count as completed)
        assert execution.steps[0].status == NodeStatus.SKIPPED
        assert execution.steps[1].status == NodeStatus.SUCCESS

    @pytest.mark.asyncio
    async def test_cancel_execution(self):
        wf = Workflow(name="可取消")  # i18n: user-facing
        wf.add_node(MockSuccessNode(node_id="n1"))

        engine = WorkflowEngine()
        # Execution starts first, but MockSuccessNode completes instantly, so cancel may be too late
        # Here we only test that the cancel method does not raise
        assert not engine.cancel("non_existent_id")

    @pytest.mark.asyncio
    async def test_progress_callback(self):
        wf = Workflow(name="回调测试")  # i18n: user-facing
        wf.add_node(MockSuccessNode(node_id="n1"))

        engine = WorkflowEngine()
        callbacks = []

        def on_progress(execution, step):
            callbacks.append(step.node_id)

        engine.on_progress(on_progress)
        await engine.execute(wf)

        assert "n1" in callbacks

    def test_list_executions(self):
        engine = WorkflowEngine()
        execs = engine.list_executions()
        assert isinstance(execs, list)


# ── Node config tests ──


class TestNodeConfig:
    def test_config_defaults(self):
        cfg = NodeConfig()
        assert cfg.label == ""
        assert cfg.continue_on_error is False
        assert cfg.max_retries == 0
        assert cfg.params == {}

    def test_config_with_params(self):
        cfg = NodeConfig(label="测试", params={"path": "~/Desktop", "recursive": True})  # i18n: user-facing
        assert cfg.label == "测试"  # i18n: user-facing
        assert cfg.params["path"] == "~/Desktop"
        assert cfg.params["recursive"] is True


# ── File IO tests ──


class TestFileIONodes:
    @pytest.mark.asyncio
    async def test_file_input_dir(self, tmp_path):
        """Test file input node reading a directory."""
        # Create temp files
        (tmp_path / "test1.txt").write_text("hello")
        (tmp_path / "test2.pdf").write_text("world")
        (tmp_path / "test3.py").write_text("print('hi')")

        node = NodeRegistry.create(
            "file_input",
            config=NodeConfig(
                params={
                    "path": str(tmp_path),
                    "recursive": False,
                    "file_patterns": "*",
                }
            ),
        )
        assert node is not None

        result = await node.execute({})
        assert result.status == NodeStatus.SUCCESS
        assert result.data["total_count"] == 3
        assert len(result.data["files"]) == 3

    @pytest.mark.asyncio
    async def test_file_input_with_pattern(self, tmp_path):
        (tmp_path / "test1.txt").write_text("hello")
        (tmp_path / "test2.pdf").write_text("world")

        node = NodeRegistry.create(
            "file_input",
            config=NodeConfig(
                params={
                    "path": str(tmp_path),
                    "file_patterns": "*.txt",
                }
            ),
        )
        assert node is not None

        result = await node.execute({})
        assert result.status == NodeStatus.SUCCESS
        assert result.data["total_count"] == 1

    @pytest.mark.asyncio
    async def test_file_output_json(self, tmp_path):
        output_file = tmp_path / "output"
        node = NodeRegistry.create(
            "file_output",
            config=NodeConfig(
                params={
                    "output_path": str(output_file),
                    "file_name": "test_output",
                    "format": "json",
                }
            ),
        )
        assert node is not None

        result = await node.execute({"data": {"key": "value", "count": 42}})
        assert result.status == NodeStatus.SUCCESS
        assert result.data["file_path"] is not None


# ── Logic node tests ──


class TestLogicNodes:
    @pytest.mark.asyncio
    async def test_filter_node(self):
        node = NodeRegistry.create(
            "filter",
            config=NodeConfig(
                params={
                    "filter_type": "extension",
                    "filter_value": ".txt,.md",
                }
            ),
        )
        assert node is not None

        files = ["a.txt", "b.pdf", "c.md", "d.jpg"]
        result = await node.execute({"data": files})
        assert result.status == NodeStatus.SUCCESS
        assert len(result.data["passed"]) == 2
        assert len(result.data["filtered"]) == 2

    @pytest.mark.asyncio
    async def test_filter_invert(self):
        node = NodeRegistry.create(
            "filter",
            config=NodeConfig(
                params={
                    "filter_type": "extension",
                    "filter_value": ".txt",
                    "invert": True,
                }
            ),
        )
        files = ["a.txt", "b.pdf", "c.md"]
        result = await node.execute({"data": files})
        assert result.status == NodeStatus.SUCCESS
        assert len(result.data["passed"]) == 2  # non-.txt files
        assert len(result.data["filtered"]) == 1  # .txt filtered out

    @pytest.mark.asyncio
    async def test_loop_node(self):
        node = NodeRegistry.create(
            "loop",
            config=NodeConfig(
                params={
                    "operation": "passthrough",
                }
            ),
        )
        result = await node.execute({"items": [1, 2, 3, 4, 5]})
        assert result.status == NodeStatus.SUCCESS
        assert result.data["total"] == 5
        assert result.data["success_count"] == 5

    @pytest.mark.asyncio
    async def test_merge_concat(self):
        node = NodeRegistry.create(
            "merge",
            config=NodeConfig(
                params={
                    "merge_mode": "concat",
                }
            ),
        )
        result = await node.execute({"data_1": [1, 2], "data_2": [3, 4]})
        assert result.status == NodeStatus.SUCCESS
        assert result.data["merged"] == [1, 2, 3, 4]


# ── Template tests ──


class TestTemplateManager:
    def test_list_templates(self):
        from fusion_cowork.templates import TemplateManager

        mgr = TemplateManager()
        templates = mgr.list_templates()
        assert len(templates) > 0
        assert any(t["id"] == "desktop_daily_cleanup" for t in templates)

    def test_get_template(self):
        from fusion_cowork.templates import TemplateManager

        mgr = TemplateManager()
        tpl = mgr.get_template("desktop_daily_cleanup")
        assert tpl is not None
        assert tpl["name"] == "桌面每日规整"  # i18n: user-facing

    def test_template_not_found(self):
        from fusion_cowork.templates import TemplateManager

        mgr = TemplateManager()
        assert mgr.get_template("non_existent") is None

    def test_template_to_workflow(self):
        from fusion_cowork.templates import TemplateManager

        mgr = TemplateManager()
        wf = mgr.template_to_workflow("desktop_daily_cleanup")
        assert wf is not None
        assert len(wf.nodes) > 0
        assert wf.id.startswith("tpl_")

    def test_get_categories(self):
        from fusion_cowork.templates import TemplateManager

        mgr = TemplateManager()
        categories = mgr.get_categories()
        assert len(categories) > 0

    def test_search_templates(self):
        from fusion_cowork.templates import TemplateManager

        mgr = TemplateManager()
        results = mgr.search_templates("桌面")  # i18n: user-facing
        assert len(results) > 0


# ── Workflow serialization tests ──


class TestWorkflowSerialization:
    def test_save_and_load_json(self, tmp_path):
        wf = Workflow(name="测试工作流")  # i18n: user-facing
        wf.add_node(MockSuccessNode(node_id="n1"))
        wf.add_node(MockTransformNode(node_id="n2", config=NodeConfig(params={"prefix": "p_"})))
        wf.connect("n1", "n2")

        json_path = tmp_path / "test_workflow.json"
        json_path.write_text(wf.to_json(), encoding="utf-8")

        loaded = Workflow.from_json(json_path.read_text(encoding="utf-8"))
        assert loaded.name == "测试工作流"  # i18n: user-facing
        assert len(loaded.nodes) == 2
        assert len(loaded.edges) == 1
        assert loaded.nodes["n2"].config.params["prefix"] == "p_"

    def test_workflow_with_tags(self):
        wf = Workflow(name="带标签", tags=["桌面", "清理"])  # i18n: user-facing
        assert "桌面" in wf.tags  # i18n: user-facing
        assert "清理" in wf.tags  # i18n: user-facing

        d = wf.to_dict()
        assert "桌面" in d["tags"]  # i18n: user-facing

    def test_edge_creation(self):
        edge = Edge(source_id="n1", target_id="n2", label="连接")  # i18n: user-facing
        assert edge.source_id == "n1"
        assert edge.target_id == "n2"
        assert edge.label == "连接"  # i18n: user-facing

        d = edge.to_dict()
        assert d["source_id"] == "n1"

        e2 = Edge.from_dict(d)
        assert e2.source_id == "n1"


# ── Node registry cleanup tests ──


class TestNodeRegistryCleanup:
    def test_unregister(self):
        # Save registry state to avoid breaking other tests
        saved_registry = dict(NodeRegistry._registry)
        NodeRegistry.register(MockSuccessNode)
        NodeRegistry.unregister("mock_success")
        assert NodeRegistry.get("mock_success") is None
        # Restore registry
        NodeRegistry._registry.clear()
        NodeRegistry._registry.update(saved_registry)


# ── macOS node tests (basic) ──


class TestMacOSNodes:
    @pytest.mark.asyncio
    async def test_file_classifier(self):
        node = NodeRegistry.create(
            "file_classifier",
            config=NodeConfig(
                params={
                    "move_to_subdirs": False,
                }
            ),
        )
        assert node is not None

        # Use temp files
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(b"test")
            pdf_path = f.name
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            f.write(b"test")
            jpg_path = f.name

        try:
            result = await node.execute({"files": [pdf_path, jpg_path]})
            assert result.status == NodeStatus.SUCCESS
            assert result.data["classified_files"][0]["category"] == "文档" or "文档" in str(  # i18n: user-facing
                result.data["categories"]
            )  # i18n: user-facing
        finally:
            os.unlink(pdf_path)
            os.unlink(jpg_path)

    @pytest.mark.asyncio
    async def test_file_batch_rename_dry_run(self):
        node = NodeRegistry.create(
            "file_batch_rename",
            config=NodeConfig(
                params={
                    "pattern": "test_{index}",
                    "start_index": 1,
                    "padding": 3,
                    "dry_run": True,
                }
            ),
        )
        assert node is not None

        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            f.write(b"test")
            tmp_path = f.name

        try:
            result = await node.execute({"files": [tmp_path]})
            assert result.status == NodeStatus.SUCCESS
            assert len(result.data["renamed_files"]) == 1
            assert result.data["renamed_files"][0]["action"] == "would_rename"
        finally:
            os.unlink(tmp_path)

    @pytest.mark.asyncio
    async def test_disk_cleaner_dry_run(self):
        node = NodeRegistry.create(
            "disk_cleaner",
            config=NodeConfig(
                params={
                    "clean_pycache": True,
                    "clean_ds_store": True,
                    "dry_run": True,
                    "max_depth": 2,
                }
            ),
        )
        assert node is not None

        result = await node.execute({"target_path": tempfile.gettempdir()})
        assert result.status == NodeStatus.SUCCESS
        assert "total_freed" in result.data


# ── AI client tests ──


class TestAIClient:
    @pytest.mark.asyncio
    async def test_mlx_client_health_check(self):
        from fusion_cowork.ai import FusionMLXClient

        client = FusionMLXClient(base_url="http://localhost:18000/v1")  # non-standard port to avoid conflicts
        try:
            health = await client.health()
            # Must not raise
            assert isinstance(health, bool)
        finally:
            await client.close()

    def test_llm_response_dataclass(self):
        from fusion_cowork.ai import LLMResponse

        resp = LLMResponse(content="Hello", tool_calls=[], finish_reason="stop")
        assert resp.content == "Hello"
        assert resp.finish_reason == "stop"


# ── Param type coercion tests (adopted from the Squish _coerce_* mechanism) ──


class TestTypeCoercion:
    """Test param type coercion (consolidated from the Squish tool_registry.py _coerce_* function family)."""

    def test_coerce_int_from_str(self):
        assert _coerce_int("42") == 42
        assert _coerce_int("-5") == -5
        assert _coerce_int("0") == 0

    def test_coerce_int_from_float(self):
        assert _coerce_int(3.14) == 3
        assert _coerce_int(3.0) == 3

    def test_coerce_int_from_bool(self):
        # bool is a subclass of int — no coercion
        assert _coerce_int(True) is True
        assert _coerce_int(False) is False

    def test_coerce_int_from_str_float(self):
        assert _coerce_int("3.14") == 3
        assert _coerce_int("3.0") == 3

    def test_coerce_int_invalid(self):
        assert _coerce_int("not_a_number") == "not_a_number"
        assert _coerce_int(None) is None

    def test_coerce_number(self):
        assert _coerce_number("3.14") == 3.14
        assert _coerce_number("42") == 42.0
        assert _coerce_number(42) == 42
        assert _coerce_number("abc") == "abc"

    def test_coerce_bool(self):
        assert _coerce_bool("true") is True
        assert _coerce_bool("false") is False
        assert _coerce_bool("yes") is True
        assert _coerce_bool("no") is False
        assert _coerce_bool(True) is True
        assert _coerce_bool(False) is False

    def test_coerce_array(self):
        assert _coerce_array("a,b,c") == ["a", "b", "c"]
        assert _coerce_array("[1,2,3]") == [1, 2, 3]
        assert _coerce_array([1, 2, 3]) == [1, 2, 3]
        assert _coerce_array("hello") == ["hello"]

    def test_coerce_param_dispatch(self):
        assert coerce_param("42", "integer") == 42
        assert coerce_param("true", "boolean") is True
        assert coerce_param("3.14", "number") == 3.14
        assert coerce_param("a,b", "array") == ["a", "b"]
        assert coerce_param('{"a":1}', "object") == {"a": 1}

    def test_coerce_params_batch(self):
        params = {"count": "5", "enabled": "true", "name": "test"}
        schema = {
            "type": "object",
            "properties": {
                "count": {"type": "integer"},
                "enabled": {"type": "boolean"},
                "name": {"type": "string"},
            },
        }
        result = coerce_params(params, schema)
        assert result["count"] == 5
        assert result["enabled"] is True
        assert result["name"] == "test"


# ── Tool node tests (adopted from the Squish built-in tool set) ──


class TestToolNodes:
    @pytest.mark.asyncio
    async def test_shell_exec_success(self):
        node = NodeRegistry.create(
            "shell_exec",
            config=NodeConfig(
                params={
                    "command": "echo 'hello world'",
                    "timeout": 5,
                    "capture_output": True,
                }
            ),
        )
        assert node is not None
        result = await node.execute({})
        assert result.status == NodeStatus.SUCCESS
        assert "hello world" in result.data.get("stdout", "")

    @pytest.mark.asyncio
    async def test_shell_exec_fail(self):
        node = NodeRegistry.create(
            "shell_exec",
            config=NodeConfig(
                params={
                    "command": "exit 1",
                    "timeout": 5,
                }
            ),
        )
        assert node is not None
        result = await node.execute({})
        assert result.status == NodeStatus.FAILED

    @pytest.mark.asyncio
    async def test_python_repl_simple(self):
        node = NodeRegistry.create(
            "python_repl",
            config=NodeConfig(
                params={
                    "code": "1 + 1",
                    "timeout": 5,
                }
            ),
        )
        assert node is not None
        result = await node.execute({})
        assert result.status == NodeStatus.SUCCESS
        assert "2" in result.data.get("result", "")

    @pytest.mark.asyncio
    async def test_python_repl_with_vars(self):
        node = NodeRegistry.create(
            "python_repl",
            config=NodeConfig(
                params={
                    "code": "x + y",
                    "variables": {"x": 10, "y": 20},
                    "timeout": 5,
                }
            ),
        )
        assert node is not None
        result = await node.execute({})
        assert result.status == NodeStatus.SUCCESS

    @pytest.mark.asyncio
    async def test_python_repl_error(self):
        node = NodeRegistry.create(
            "python_repl",
            config=NodeConfig(
                params={
                    "code": "1/0",
                    "timeout": 5,
                }
            ),
        )
        assert node is not None
        result = await node.execute({})
        assert result.status == NodeStatus.FAILED

    @pytest.mark.asyncio
    async def test_apply_edit_dry_run(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("hello world\nhello fusion")
            tmp_path = f.name

        try:
            node = NodeRegistry.create(
                "apply_edit",
                config=NodeConfig(
                    params={
                        "file_path": tmp_path,
                        "old_text": "hello",
                        "new_text": "hi",
                        "dry_run": True,
                    }
                ),
            )
            assert node is not None
            result = await node.execute({})
            assert result.status == NodeStatus.SUCCESS
            assert result.data["changes"] == 2
        finally:
            os.unlink(tmp_path)

    @pytest.mark.asyncio
    async def test_apply_edit_actual(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("old content")
            tmp_path = f.name

        try:
            node = NodeRegistry.create(
                "apply_edit",
                config=NodeConfig(
                    params={
                        "file_path": tmp_path,
                        "old_text": "old",
                        "new_text": "new",
                        "dry_run": False,
                        "create_backup": False,
                    }
                ),
            )
            assert node is not None
            result = await node.execute({})
            assert result.status == NodeStatus.SUCCESS
            assert result.data["changes"] == 1
            # Verify file content was updated
            content = Path(tmp_path).read_text()
            assert "new content" in content
        finally:
            os.unlink(tmp_path)


# ── Alias resolution tests (adopted from Squish tool_name_map.py) ──


class TestNodeAliases:
    def test_register_alias(self):
        # Test alias registration
        NodeRegistry.register_alias("测试别名", "mock_success")  # i18n: user-facing
        assert NodeRegistry.get("测试别名") is not None  # i18n: user-facing

    def test_alias_lookup(self):
        NodeRegistry.register_alias("成功节点", "mock_success")  # i18n: user-facing
        cls = NodeRegistry.get("成功节点")  # i18n: user-facing
        assert cls is not None
        assert cls.name == "mock_success"

    def test_resolve_alias(self):
        NodeRegistry.register_alias("resolved_alias", "mock_success")
        resolved = NodeRegistry.resolve_alias("resolved_alias")
        assert resolved == "mock_success"

    def test_resolve_alias_unknown(self):
        resolved = NodeRegistry.resolve_alias("unknown_name")
        assert resolved == "unknown_name"  # unknown name returned as-is

    def test_resolve_alias_direct(self):
        resolved = NodeRegistry.resolve_alias("mock_success")
        assert resolved == "mock_success"  # direct name returned as-is


# ── Lazy Import tests (adopted from the Squish __getattr__ mechanism) ──


class TestLazyImport:
    def test_lazy_import_workflow(self):
        import fusion_cowork

        wf = fusion_cowork.Workflow(name="延迟导入测试")  # i18n: user-facing
        assert wf.name == "延迟导入测试"  # i18n: user-facing

    def test_lazy_import_node(self):
        import fusion_cowork

        node = fusion_cowork.DesktopCleanNode()
        assert node.name == "desktop_clean"

    def test_lazy_import_ai(self):
        import fusion_cowork

        client = fusion_cowork.FusionMLXClient()
        assert client is not None

    def test_lazy_import_tool_node(self):
        import fusion_cowork

        node = fusion_cowork.ShellExecNode()
        assert node.name == "shell_exec"

    def test_lazy_import_template(self):
        import fusion_cowork

        mgr = fusion_cowork.TemplateManager()
        assert mgr is not None

    def test_lazy_import_nonexistent(self):
        import fusion_cowork

        with pytest.raises(AttributeError):
            _ = fusion_cowork.NonExistentName

    def test_lazy_import_cached(self):
        import fusion_cowork

        # Second access should use the cache
        wf1 = fusion_cowork.Workflow
        wf2 = fusion_cowork.Workflow
        assert wf1 is wf2  # same object
