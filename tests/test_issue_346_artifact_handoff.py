"""issue #346: product_lifecycle 规范产物交接 — 下游座位按 artifact id 取 PRD, 不猜路径。

agent-studio run result 的 artifacts 是 id 字符串列表 (非对象), 模板把
${step.n1.result.artifacts.0} 插进架构/env-pack 座位 prompt 作规范引用;
BA 无产物时按 #343 语义明确 unresolved 报错, 不静默猜路径。
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
class _StudioBaNode(BaseNode):
    name = "studio_ba_346"
    display_name = "Studio BA 346"
    category = NodeCategory.TOOL
    description = "测试用: agent-studio run result 形态 (artifacts id 列表 + last_result.report)"
    icon = "📝"
    default_label = "BA"

    with_artifacts = True

    async def execute(self, inputs: dict) -> NodeResult:
        artifacts = ["8d7baf6a616c"] if type(self).with_artifacts else []
        return NodeResult(
            status=NodeStatus.SUCCESS,
            data={
                "run_id": "run-x",
                "status": "completed",
                "result": {
                    "run_id": "run-x",
                    "status": "completed",
                    "artifacts": artifacts,
                    "last_result": {"report": "PRD-BODY"},
                },
            },
            summary="ok",
        )


@register_node
class _ConsumeNode(BaseNode):
    name = "consume_346"
    display_name = "Consume 346"
    category = NodeCategory.TOOL
    description = "测试用: 记录收到的参数"
    icon = "📥"
    default_label = "Consume"

    seen: list = []

    async def execute(self, inputs: dict) -> NodeResult:
        type(self).seen.append({"params": dict(self.config.params or {})})
        return NodeResult(status=NodeStatus.SUCCESS, data={"ok": True}, summary="ok")


class TestArtifactHandoffTemplate:
    def test_architect_prompt_carries_canonical_artifact_ref(self):
        # n3 架构座位 prompt 传入 BA 产物 artifact id 规范引用
        nodes = {n["id"]: n for n in _tpl()["workflow"]["nodes"]}
        n3_input = nodes["n3"]["config"]["params"]["input"]
        assert "${step.n1.result.artifacts.0}" in n3_input
        assert "canonical reference" in n3_input

    def test_env_pack_prompt_carries_both_refs(self):
        nodes = {n["id"]: n for n in _tpl()["workflow"]["nodes"]}
        n9_input = nodes["n9"]["config"]["params"]["input"]
        assert "${step.n1.result.artifacts.0}" in n9_input
        assert "${step.n3.result.artifacts.0}" in n9_input

    def test_assembly_preserves_artifact_refs(self):
        # ${param.*} 装配期替换, ${step.*} artifact 引用留给引擎运行期
        wf = TemplateManager().template_to_workflow(
            "product_lifecycle", param_values={"slug": "demo", "repo": "o/r", "task_title": "t"}
        )
        assert "${step.n1.result.artifacts.0}" in wf.nodes["n3"].config.params["input"]
        assert "${step.n3.result.artifacts.0}" in wf.nodes["n9"].config.params["input"]


class TestArtifactRefInterpolation:
    async def test_artifact_id_resolves_through_list_index(self):
        # 引擎端到端: run result 形态产出 → 下游参数按列表下标取 artifact id
        _ConsumeNode.seen = []
        eng = WorkflowEngine()
        wf = Workflow(name="interp-346")
        n1 = NodeRegistry.create("studio_ba_346")
        n1.id = "n1"
        n2 = NodeRegistry.create("consume_346")
        n2.id = "n2"
        n2.config = NodeConfig(
            params={"prd_artifact": "PRD artifact id (canonical reference):\n${step.n1.result.artifacts.0}"}
        )
        wf.add_node(n1)
        wf.add_node(n2)
        wf.connect("n1", "n2")
        execution = await eng.execute(wf, execution_id="run-346-artifact")
        assert execution.status.value == "success"
        assert _ConsumeNode.seen[-1]["params"]["prd_artifact"] == "PRD artifact id (canonical reference):\n8d7baf6a616c"

    async def test_missing_artifact_fails_loud_not_silent(self):
        # BA 无产物 → 明确 unresolved 报错 (不静默, 下游不再靠运气猜路径)
        _ConsumeNode.seen = []
        _StudioBaNode.with_artifacts = False
        try:
            eng = WorkflowEngine()
            wf = Workflow(name="interp-346-miss")
            n1 = NodeRegistry.create("studio_ba_346")
            n1.id = "n1"
            n2 = NodeRegistry.create("consume_346")
            n2.id = "n2"
            n2.config = NodeConfig(params={"prd_artifact": "${step.n1.result.artifacts.0}"})
            wf.add_node(n1)
            wf.add_node(n2)
            wf.connect("n1", "n2")
            execution = await eng.execute(wf, execution_id="run-346-miss")
            assert execution.status.value == "failed"
            assert "unresolved interpolation" in (execution.error or "")
        finally:
            _StudioBaNode.with_artifacts = True


def _tpl():
    return next(t for t in BUILTIN_TEMPLATES if t["id"] == "product_lifecycle")
