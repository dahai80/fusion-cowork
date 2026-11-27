"""A-1 port routing tests — edge.source_output/target_input lands values by port for non-default ports."""

from __future__ import annotations

import json

import pytest

from fusion_cowork.engine.node import (
    BaseNode,
    NodeCategory,
    NodeResult,
    NodeStatus,
    register_node,
)
from fusion_cowork.engine.workflow import Workflow, WorkflowEngine, WorkflowStatus


@register_node
class MockFanoutNode(BaseNode):
    """Fanout node: outputs multiple named ports."""

    name = "mock_fanout"
    display_name = "Mock 扇出"  # i18n: user-facing
    category = NodeCategory.IO
    description = "测试用: 多端口输出"  # i18n: user-facing
    icon = "🧪"
    default_label = "Mock 扇出"  # i18n: user-facing

    def get_params_schema(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, inputs: dict) -> NodeResult:
        return NodeResult(
            status=NodeStatus.SUCCESS,
            data={"alpha": "A值", "beta": "B值", "output": "默认全量"},  # i18n: user-facing
            summary="扇出",  # i18n: user-facing
        )


@register_node
class MockPortReaderNode(BaseNode):
    """Node that reads the target_input key."""

    name = "mock_port_reader"
    display_name = "Mock 端口读取"  # i18n: user-facing
    category = NodeCategory.IO
    description = "测试用: 读指定输入键"  # i18n: user-facing
    icon = "🧪"
    default_label = "Mock 端口读取"  # i18n: user-facing

    def get_params_schema(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, inputs: dict) -> NodeResult:
        return NodeResult(
            status=NodeStatus.SUCCESS,
            data={"received": json.dumps(inputs, ensure_ascii=False)},
            summary="读取",  # i18n: user-facing
        )


def _build_wf(edges):
    wf = Workflow(name="端口路由测试")  # i18n: user-facing
    wf.add_node(MockFanoutNode(node_id="src1"))
    wf.add_node(MockFanoutNode(node_id="src2"))
    wf.add_node(MockPortReaderNode(node_id="dst"))
    for e in edges:
        wf.connect(*e)
    return wf


def _received(exec_result):
    out = exec_result.steps[-1].output_data
    return json.loads(out["received"])


@pytest.mark.asyncio
async def test_default_edge_full_merge_backward_compat():
    # Default edge (output→input): full merge of upstream, existing nodes reading inputs.get("data") still work
    wf = Workflow(name="默认合并")  # i18n: user-facing
    wf.add_node(MockFanoutNode(node_id="src"))
    wf.add_node(MockPortReaderNode(node_id="dst"))
    wf.connect("src", "dst")  # default output→input

    engine = WorkflowEngine()
    exec_result = await engine.execute(wf)
    assert exec_result.status == WorkflowStatus.SUCCESS
    received = _received(exec_result)
    # Default merge: all upstream keys (alpha/beta/output) should appear in dst inputs
    assert received.get("alpha") == "A值"  # i18n: user-facing
    assert received.get("beta") == "B值"  # i18n: user-facing
    assert received.get("output") == "默认全量"  # i18n: user-facing


@pytest.mark.asyncio
async def test_nondefault_port_routes_specific_output():
    # Explicit port edge (source_output=alpha → target_input=left_in): only alpha lands in left_in
    wf = Workflow(name="端口路由")  # i18n: user-facing
    wf.add_node(MockFanoutNode(node_id="src"))
    wf.add_node(MockPortReaderNode(node_id="dst"))
    wf.connect("src", "dst", source_output="alpha", target_input="left_in")

    engine = WorkflowEngine()
    exec_result = await engine.execute(wf)
    assert exec_result.status == WorkflowStatus.SUCCESS
    received = _received(exec_result)
    assert received.get("left_in") == "A值", "非默认端口应按 source_output 取值落 target_input"  # i18n: user-facing
    # Keys from the default full merge must not leak in
    assert "beta" not in received, "非指定端口值不应出现"  # i18n: user-facing
    assert "alpha" not in received, "原端口名不应残留 (已路由到 left_in)"  # i18n: user-facing


@pytest.mark.asyncio
async def test_two_fanout_edges_no_cross_contamination():
    # Two fanout sources routed to different target_inputs, no cross-contamination
    wf = Workflow(name="双源端口")  # i18n: user-facing
    wf.add_node(MockFanoutNode(node_id="s1"))
    wf.add_node(MockFanoutNode(node_id="s2"))
    wf.add_node(MockPortReaderNode(node_id="dst"))
    wf.connect("s1", "dst", source_output="alpha", target_input="in1")
    wf.connect("s2", "dst", source_output="beta", target_input="in2")

    engine = WorkflowEngine()
    exec_result = await engine.execute(wf)
    assert exec_result.status == WorkflowStatus.SUCCESS
    received = _received(exec_result)
    assert received.get("in1") == "A值"  # i18n: user-facing
    assert received.get("in2") == "B值"  # i18n: user-facing
    assert "alpha" not in received and "beta" not in received, "原端口名不应残留"  # i18n: user-facing


@pytest.mark.asyncio
async def test_missing_source_output_key_skipped():
    # source_output pointing at a missing key → val=None → skipped (no empty key landed)
    wf = Workflow(name="缺失端口")  # i18n: user-facing
    wf.add_node(MockFanoutNode(node_id="src"))
    wf.add_node(MockPortReaderNode(node_id="dst"))
    wf.connect("src", "dst", source_output="nonexistent", target_input="in_x")

    engine = WorkflowEngine()
    exec_result = await engine.execute(wf)
    assert exec_result.status == WorkflowStatus.SUCCESS
    received = _received(exec_result)
    assert "in_x" not in received, "源端口无值时应跳过, 不落 None"  # i18n: user-facing


@pytest.mark.asyncio
async def test_mixed_default_and_explicit_edges():
    # One default merge + one explicit port → both behaviors coexist
    wf = Workflow(name="混合端口")  # i18n: user-facing
    wf.add_node(MockFanoutNode(node_id="s1"))
    wf.add_node(MockFanoutNode(node_id="s2"))
    wf.add_node(MockPortReaderNode(node_id="dst"))
    wf.connect("s1", "dst")  # default full merge from s1
    wf.connect("s2", "dst", source_output="beta", target_input="from_s2")  # only take s2.beta

    engine = WorkflowEngine()
    exec_result = await engine.execute(wf)
    assert exec_result.status == WorkflowStatus.SUCCESS
    received = _received(exec_result)
    # s1 default merge: alpha/beta/output all present
    assert received.get("alpha") == "A值"  # i18n: user-facing
    assert received.get("output") == "默认全量"  # i18n: user-facing
    # s2 explicit port: beta routed to from_s2 (overrides s1's default-merged beta, last-write-wins)
    assert received.get("from_s2") == "B值"  # i18n: user-facing
