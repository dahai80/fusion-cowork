"""A-1 端口路由测试 — edge.source_output/target_input 非默认端口按端口取值落键。"""

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
    """扇出节点: 输出多个命名端口。"""

    name = "mock_fanout"
    display_name = "Mock 扇出"
    category = NodeCategory.IO
    description = "测试用: 多端口输出"
    icon = "🧪"
    default_label = "Mock 扇出"

    def get_params_schema(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, inputs: dict) -> NodeResult:
        return NodeResult(
            status=NodeStatus.SUCCESS,
            data={"alpha": "A值", "beta": "B值", "output": "默认全量"},
            summary="扇出",
        )


@register_node
class MockPortReaderNode(BaseNode):
    """读 target_input 键的节点。"""

    name = "mock_port_reader"
    display_name = "Mock 端口读取"
    category = NodeCategory.IO
    description = "测试用: 读指定输入键"
    icon = "🧪"
    default_label = "Mock 端口读取"

    def get_params_schema(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, inputs: dict) -> NodeResult:
        return NodeResult(
            status=NodeStatus.SUCCESS,
            data={"received": json.dumps(inputs, ensure_ascii=False)},
            summary="读取",
        )


def _build_wf(edges):
    wf = Workflow(name="端口路由测试")
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
    # 默认 edge (output→input): 全量合并上游, 现有节点读 inputs.get("data") 仍工作
    wf = Workflow(name="默认合并")
    wf.add_node(MockFanoutNode(node_id="src"))
    wf.add_node(MockPortReaderNode(node_id="dst"))
    wf.connect("src", "dst")  # 默认 output→input

    engine = WorkflowEngine()
    exec_result = await engine.execute(wf)
    assert exec_result.status == WorkflowStatus.SUCCESS
    received = _received(exec_result)
    # 默认合并: 上游全部键 (alpha/beta/output) 都应出现在 dst 输入
    assert received.get("alpha") == "A值"
    assert received.get("beta") == "B值"
    assert received.get("output") == "默认全量"


@pytest.mark.asyncio
async def test_nondefault_port_routes_specific_output():
    # 显式端口 edge (source_output=alpha → target_input=left_in): 只落 alpha 值到 left_in
    wf = Workflow(name="端口路由")
    wf.add_node(MockFanoutNode(node_id="src"))
    wf.add_node(MockPortReaderNode(node_id="dst"))
    wf.connect("src", "dst", source_output="alpha", target_input="left_in")

    engine = WorkflowEngine()
    exec_result = await engine.execute(wf)
    assert exec_result.status == WorkflowStatus.SUCCESS
    received = _received(exec_result)
    assert received.get("left_in") == "A值", "非默认端口应按 source_output 取值落 target_input"
    # 默认全合并的键不应污染进来
    assert "beta" not in received, "非指定端口值不应出现"
    assert "alpha" not in received, "原端口名不应残留 (已路由到 left_in)"


@pytest.mark.asyncio
async def test_two_fanout_edges_no_cross_contamination():
    # 两个扇出源分别路由到不同 target_input, 互不污染
    wf = Workflow(name="双源端口")
    wf.add_node(MockFanoutNode(node_id="s1"))
    wf.add_node(MockFanoutNode(node_id="s2"))
    wf.add_node(MockPortReaderNode(node_id="dst"))
    wf.connect("s1", "dst", source_output="alpha", target_input="in1")
    wf.connect("s2", "dst", source_output="beta", target_input="in2")

    engine = WorkflowEngine()
    exec_result = await engine.execute(wf)
    assert exec_result.status == WorkflowStatus.SUCCESS
    received = _received(exec_result)
    assert received.get("in1") == "A值"
    assert received.get("in2") == "B值"
    assert "alpha" not in received and "beta" not in received, "原端口名不应残留"


@pytest.mark.asyncio
async def test_missing_source_output_key_skipped():
    # source_output 指向不存在的键 → val=None → 跳过 (不落空键)
    wf = Workflow(name="缺失端口")
    wf.add_node(MockFanoutNode(node_id="src"))
    wf.add_node(MockPortReaderNode(node_id="dst"))
    wf.connect("src", "dst", source_output="nonexistent", target_input="in_x")

    engine = WorkflowEngine()
    exec_result = await engine.execute(wf)
    assert exec_result.status == WorkflowStatus.SUCCESS
    received = _received(exec_result)
    assert "in_x" not in received, "源端口无值时应跳过, 不落 None"


@pytest.mark.asyncio
async def test_mixed_default_and_explicit_edges():
    # 一条默认合并 + 一条显式端口 → 两类行为共存
    wf = Workflow(name="混合端口")
    wf.add_node(MockFanoutNode(node_id="s1"))
    wf.add_node(MockFanoutNode(node_id="s2"))
    wf.add_node(MockPortReaderNode(node_id="dst"))
    wf.connect("s1", "dst")  # 默认全合并 s1
    wf.connect("s2", "dst", source_output="beta", target_input="from_s2")  # 只取 s2.beta

    engine = WorkflowEngine()
    exec_result = await engine.execute(wf)
    assert exec_result.status == WorkflowStatus.SUCCESS
    received = _received(exec_result)
    # s1 默认合并: alpha/beta/output 都在
    assert received.get("alpha") == "A值"
    assert received.get("output") == "默认全量"
    # s2 显式端口: beta 路由到 from_s2 (覆盖 s1 默认合并的 beta, 因为后到)
    assert received.get("from_s2") == "B值"
