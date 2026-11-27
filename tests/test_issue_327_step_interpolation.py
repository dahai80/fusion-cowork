"""issue #327: engine step-ref interpolation — ${step.<id>.<path>} / ${result.<path>}.

Upstream node output (PRD at result.last_result.report) must reach downstream
node params; template product_lifecycle wires n1 PRD into n3/n9 goals.
"""

from __future__ import annotations

from fusion_cowork.engine.node import (
    BaseNode,
    NodeCategory,
    NodeConfig,
    NodeRegistry,
    NodeResult,
    NodeStatus,
    register_node,
)
from fusion_cowork.engine.workflow import Workflow, WorkflowEngine
from fusion_cowork.nodes import import_all_nodes
from fusion_cowork.templates.template_manager import BUILTIN_TEMPLATES, TemplateManager

import_all_nodes()


@register_node
class _ProduceNode(BaseNode):
    name = "produce_327"
    display_name = "Produce 327"
    category = NodeCategory.TOOL
    description = "测试用: 产出 PRD 形状数据"  # i18n: user-facing
    icon = "📦"
    default_label = "Produce"

    async def execute(self, inputs: dict) -> NodeResult:
        return NodeResult(
            status=NodeStatus.SUCCESS,
            data={
                "run_id": "stub",
                "status": "completed",
                "result": {"last_result": {"report": "PRD-BODY-CONTENT"}},
                "repo_path": "/ws/ai-x",
                "number": 42,
            },
            summary="ok",
        )


@register_node
class _PassthroughNode(BaseNode):
    name = "passthrough_327"
    display_name = "Passthrough 327"
    category = NodeCategory.TOOL
    description = "测试用: 闸门位直通 (模拟 approval_gate 挡在中间)"  # i18n: user-facing
    icon = "➡️"
    default_label = "Passthrough"

    async def execute(self, inputs: dict) -> NodeResult:
        return NodeResult(status=NodeStatus.SUCCESS, data={"approved": True}, summary="ok")


@register_node
class _ConsumeNode(BaseNode):
    name = "consume_327"
    display_name = "Consume 327"
    category = NodeCategory.TOOL
    description = "测试用: 记录收到的输入"  # i18n: user-facing
    icon = "📥"
    default_label = "Consume"

    seen: list = []

    async def execute(self, inputs: dict) -> NodeResult:
        type(self).seen.append({"inputs": dict(inputs), "params": dict(self.config.params or {})})
        return NodeResult(status=NodeStatus.SUCCESS, data={"ok": True}, summary="ok")


def _engine_wf(param_map: dict) -> Workflow:
    wf = Workflow(name="interp-327")
    n1 = NodeRegistry.create("produce_327")
    n1.id = "n1"
    n2 = NodeRegistry.create("passthrough_327")
    n2.id = "n2"
    n3 = NodeRegistry.create("consume_327")
    n3.id = "n3"
    n3.config = NodeConfig(params=param_map)
    wf.add_node(n1)
    wf.add_node(n2)
    wf.add_node(n3)
    wf.connect("n1", "n2")
    wf.connect("n2", "n3")
    return wf


class TestStepRefInterpolation:
    async def test_step_ref_resolves_through_gate(self):
        # Key scenario: n1 -> n2(gate) -> n3, n3 params explicitly reference the n1 artifact —
        # not relying on default edge merging, the reference is taken directly from passed_data
        _ConsumeNode.seen = []
        eng = WorkflowEngine()
        wf = _engine_wf({"goal": "arch per ${step.n1.result.last_result.report}"})
        execution = await eng.execute(wf, execution_id="run-327-step")
        assert execution.status.value == "success"
        assert _ConsumeNode.seen[-1]["params"]["goal"] == "arch per PRD-BODY-CONTENT"

    async def test_result_shorthand_resolves_from_merged_input(self):
        # ${result.*} shorthand reads the merged upstream input — available when producer and consumer are directly connected
        _ConsumeNode.seen = []
        eng = WorkflowEngine()
        wf = Workflow(name="interp-327-short")
        n1 = NodeRegistry.create("produce_327")
        n1.id = "n1"
        n2 = NodeRegistry.create("consume_327")
        n2.id = "n2"
        n2.config = NodeConfig(params={"body": "scaffold at ${result.repo_path}"})
        wf.add_node(n1)
        wf.add_node(n2)
        wf.connect("n1", "n2")
        await eng.execute(wf, execution_id="run-327-result")
        assert _ConsumeNode.seen[-1]["params"]["body"] == "scaffold at /ws/ai-x"

    async def test_whole_string_ref_keeps_type(self):
        _ConsumeNode.seen = []
        eng = WorkflowEngine()
        wf = _engine_wf({"issue_number": "${step.n1.number}"})
        await eng.execute(wf, execution_id="run-327-int")
        assert _ConsumeNode.seen[-1]["params"]["issue_number"] == 42
        assert isinstance(_ConsumeNode.seen[-1]["params"]["issue_number"], int)

    async def test_unresolved_ref_fails_loud(self):
        # issue #343: unmatched references no longer leave literals into the node — explicit unresolved interpolation error
        _ConsumeNode.seen = []
        eng = WorkflowEngine()
        wf = _engine_wf({"goal": "missing ${step.n1.nope.deep}"})
        execution = await eng.execute(wf, execution_id="run-327-miss")
        assert _ConsumeNode.seen == []
        assert execution.status.value == "failed"
        assert "unresolved interpolation" in execution.error

    async def test_ref_inside_list_param(self):
        _ConsumeNode.seen = []
        eng = WorkflowEngine()
        wf = _engine_wf({"context_links": ["${step.n1.repo_path}/CLAUDE.md"]})
        await eng.execute(wf, execution_id="run-327-list")
        assert _ConsumeNode.seen[-1]["params"]["context_links"] == ["/ws/ai-x/CLAUDE.md"]

    async def test_step_ref_in_inputs_not_just_params(self):
        # References can also be written on the node input side (inputs) — strings merged in via default edges are parsed the same way
        _ConsumeNode.seen = []
        eng = WorkflowEngine()
        wf = Workflow(name="interp-327-input")
        n1 = NodeRegistry.create("produce_327")
        n1.id = "n1"
        n2 = NodeRegistry.create("consume_327")
        n2.id = "n2"
        n2.config = NodeConfig(params={})
        wf.add_node(n1)
        wf.add_node(n2)
        wf.connect("n1", "n2")
        await eng.execute(wf, execution_id="run-327-inputref")
        # n1 output merges into n2 inputs via the default edge; the step self-reference in inputs resolves to its own artifact
        assert _ConsumeNode.seen[-1]["inputs"]["result"]["last_result"]["report"] == "PRD-BODY-CONTENT"


class TestProductLifecycleWiring:
    def test_template_inputs_carry_step_refs(self):
        nodes = {n["id"]: n for n in _tpl()["workflow"]["nodes"]}
        n3_input = nodes["n3"]["config"]["params"]["input"]
        n9_input = nodes["n9"]["config"]["params"]["input"]
        assert "${step.n1.result.last_result.report}" in n3_input
        assert "${step.n1.result.last_result.report}" in n9_input
        assert "${step.n3.result.last_result.report}" in n9_input

    def test_assembly_preserves_step_refs(self):
        # ${param.*} is replaced at assembly time, ${step.*} is left for engine runtime resolution — the two do not interfere
        wf = TemplateManager().template_to_workflow(
            "product_lifecycle", param_values={"slug": "demo", "repo": "o/r", "task_title": "t"}
        )
        n3 = wf.nodes["n3"]
        assert "${step.n1.result.last_result.report}" in n3.config.params["input"]
        assert "demo" in n3.config.params["input"]


def _tpl():
    return next(t for t in BUILTIN_TEMPLATES if t["id"] == "product_lifecycle")
