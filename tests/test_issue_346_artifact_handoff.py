"""issue #346: product_lifecycle canonical artifact handoff — downstream seats fetch the PRD by artifact id, no path guessing.

agent-studio run result artifacts are a list of id strings (not objects); the template inserts
${step.n1.result.artifacts.0} into the architect/env-pack seat prompts as the canonical reference;
when the BA has no artifacts, #343 semantics fail loudly with an unresolved error instead of
silently guessing a path.
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
    description = "测试用: agent-studio run result 形态 (artifacts id 列表 + last_result.report)"  # i18n: user-facing
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
    description = "测试用: 记录收到的参数"  # i18n: user-facing
    icon = "📥"
    default_label = "Consume"

    seen: list = []

    async def execute(self, inputs: dict) -> NodeResult:
        type(self).seen.append({"params": dict(self.config.params or {})})
        return NodeResult(status=NodeStatus.SUCCESS, data={"ok": True}, summary="ok")


class TestArtifactHandoffTemplate:
    def test_architect_prompt_carries_canonical_artifact_ref(self):
        # n3 architect seat prompt receives the BA artifact's artifact-id canonical reference
        nodes = {n["id"]: n for n in _tpl()["workflow"]["nodes"]}
        n3_input = nodes["n3"]["config"]["params"]["input"]
        assert "${step.n1.result.artifacts.0" in n3_input
        assert "canonical reference" in n3_input

    def test_env_pack_prompt_carries_both_refs(self):
        nodes = {n["id"]: n for n in _tpl()["workflow"]["nodes"]}
        n9_input = nodes["n9"]["config"]["params"]["input"]
        assert "${step.n1.result.artifacts.0" in n9_input
        assert "${step.n3.result.artifacts.0" in n9_input

    def test_assembly_preserves_artifact_refs(self):
        # ${param.*} is replaced at assembly time, ${step.*} artifact refs are left for engine runtime
        wf = TemplateManager().template_to_workflow(
            "product_lifecycle", param_values={"slug": "demo", "repo": "o/r", "task_title": "t"}
        )
        assert "${step.n1.result.artifacts.0" in wf.nodes["n3"].config.params["input"]
        assert "${step.n3.result.artifacts.0" in wf.nodes["n9"].config.params["input"]


class TestArtifactRefInterpolation:
    async def test_artifact_id_resolves_through_list_index(self):
        # Engine end-to-end: run-result-shaped output → downstream param takes the artifact id by list index
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
        # BA has no artifacts → explicit unresolved error (loud, downstream no longer guesses paths by luck)
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
