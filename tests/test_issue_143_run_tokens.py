"""Issue #143 回归测试 — run 瀑布 per-node token + run 头部总 token。

- _extract_tokens: usage dict 提取/缺省/零值
- 引擎执行: 节点输出 usage → step.tokens
- _step_dict 暴露 tokens (SimpleNamespace 兼容)
- AI 节点 (classify): 跨批次 usage 聚合落输出
"""

from __future__ import annotations

import asyncio


def test_extract_tokens_basic():
    from fusion_cowork.engine.workflow import _extract_tokens

    t = _extract_tokens({"usage": {"prompt_tokens": 10, "completion_tokens": 5}})
    assert t == {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    t2 = _extract_tokens({"usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 99}})
    assert t2["total_tokens"] == 99
    assert _extract_tokens({"foo": 1}) is None
    assert _extract_tokens({"usage": {}}) is None
    assert _extract_tokens({"usage": {"prompt_tokens": 0, "completion_tokens": 0}}) is None
    assert _extract_tokens("not-a-dict") is None


def test_workflow_step_tokens_default():
    from fusion_cowork.engine.workflow import WorkflowStep

    s = WorkflowStep(node_id="n1", node_name="x", node_display_name="x", status=None)
    assert s.tokens is None


def test_engine_captures_step_tokens():
    from fusion_cowork.engine.node import BaseNode, NodeCategory, NodeResult, NodeStatus, register_node
    from fusion_cowork.engine.workflow import Workflow, WorkflowEngine

    @register_node
    class Issue143UsageNode(BaseNode):
        name = "issue143_usage_node"
        display_name = "TokenMock"
        category = NodeCategory.TOOL
        description = "issue #143 token capture mock"
        icon = "T"
        default_label = "TokenMock"

        async def execute(self, inputs):
            return NodeResult(
                status=NodeStatus.SUCCESS,
                data={"answer": "ok", "usage": {"prompt_tokens": 12, "completion_tokens": 8}},
                summary="done",
            )

    engine = WorkflowEngine()
    wf = Workflow.from_dict(
        {"name": "wf-tok", "nodes": {"n1": {"name": "issue143_usage_node"}}, "edges": []}
    )
    execution = asyncio.run(engine.execute(wf))
    assert execution.steps, "应有步骤记录"
    tok = execution.steps[0].tokens
    assert tok is not None
    assert tok["prompt_tokens"] == 12 and tok["completion_tokens"] == 8 and tok["total_tokens"] == 20


def test_step_dict_exposes_tokens():
    from types import SimpleNamespace

    from fusion_cowork.server.web_api import RunRegistry

    step = SimpleNamespace(
        node_id="n1",
        node_name="issue143_usage_node",
        node_display_name="TokenMock",
        status=None,
        execution_time=0.5,
        summary="",
        error=None,
        output_data={},
        input_data={},
        tokens={"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20},
    )
    d = RunRegistry._step_dict(step)
    assert d["tokens"]["total_tokens"] == 20
    legacy = SimpleNamespace(
        node_id="n1",
        node_name="x",
        node_display_name="x",
        status=None,
        execution_time=0.1,
        summary="",
        error=None,
        output_data={},
        input_data={},
    )
    assert RunRegistry._step_dict(legacy)["tokens"] is None


class _FakeUsage:
    def __init__(self, usage):
        self.content = "```json\n[{\"file_name\": \"a.txt\", \"category\": \"txt_files\", \"confidence\": 0.9, \"reason\": \"r\"}]\n```"
        self.usage = usage


class _FakeMLX:
    def __init__(self, usage):
        self.usage = usage
        self.calls = 0

    async def chat(self, **kwargs):
        self.calls += 1
        return _FakeUsage(self.usage)


def test_classify_node_aggregates_usage():
    from fusion_cowork.engine.node import NodeConfig
    from fusion_cowork.nodes.ai.classify import AIClassifyNode

    node = AIClassifyNode(
        node_id="n1", config=NodeConfig(params={"model": "m", "max_files_per_batch": 1})
    )
    node._mlx = _FakeMLX({"prompt_tokens": 7, "completion_tokens": 3})
    files = ["/tmp/a.txt", "/tmp/b.txt"]
    result = asyncio.run(node.execute({"files": files}))
    assert result.status.value == "success"
    usage = result.data["usage"]
    assert usage["prompt_tokens"] == 14 and usage["completion_tokens"] == 6
    assert node._mlx.calls == 2
