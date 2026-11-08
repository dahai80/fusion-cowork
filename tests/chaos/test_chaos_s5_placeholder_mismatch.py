"""Chaos S5: template placeholder referencing an unknown field (issue #386, spec §3.2 row 5).

Source defect: n12 `${result.number}` vs upstream `{pr_number}` shape —
the reference silently stayed a literal or produced a confusing
int()/float() crash downstream. Assertions: the engine fails the node
fast, the error names the unresolved reference AND lists the available
upstream fields (issue #389 fail-fast contract), and the failing node
never runs with a literal placeholder baked into params.
"""

from __future__ import annotations

import pytest

from fusion_cowork.engine.node import (
    BaseNode,
    NodeCategory,
    NodeConfig,
    NodeResult,
    NodeStatus,
    register_node,
)
from fusion_cowork.engine.workflow import Workflow, WorkflowEngine, WorkflowStatus

pytestmark = [pytest.mark.chaos]


@register_node
class _ProducerNode(BaseNode):
    name = "chaos_s5_producer"
    display_name = "Chaos S5 producer"
    category = NodeCategory.IO
    description = "emits pr_number (the real upstream shape)"

    async def execute(self, inputs: dict) -> NodeResult:
        return NodeResult(status=NodeStatus.SUCCESS, data={"pr_number": 7, "pr_url": "u"}, summary="ok")


@register_node
class _ConsumerNode(BaseNode):
    name = "chaos_s5_consumer"
    display_name = "Chaos S5 consumer"
    category = NodeCategory.IO
    description = "reads issue_number param"

    async def execute(self, inputs: dict) -> NodeResult:
        return NodeResult(status=NodeStatus.SUCCESS, data={}, summary="ok")


def _workflow() -> Workflow:
    wf = Workflow(name="chaos-s5")
    wf.add_node(_ProducerNode(node_id="n1"))
    # n12-class mismatch: upstream produces pr_number, template says ${result.number}
    wf.add_node(
        _ConsumerNode(
            node_id="n2",
            config=NodeConfig(params={"issue_number": "${result.number}", "repo": "${param.repo}"}),
        )
    )
    wf.connect("n1", "n2")
    return wf


class TestPlaceholderMismatch:
    @pytest.mark.asyncio
    async def test_unknown_field_fails_fast_with_available_fields(self):
        engine = WorkflowEngine()
        execution = await engine.execute(_workflow())
        assert execution.status == WorkflowStatus.FAILED
        n2 = next(s for s in execution.steps if s.node_id == "n2")
        assert n2.status == NodeStatus.FAILED
        assert "unresolved interpolation" in (n2.error or "")
        assert "${result.number}" in (n2.error or "")
        assert "pr_number" in (n2.error or ""), "error must list available upstream fields"
        assert "available fields" in (n2.error or "")

    @pytest.mark.asyncio
    async def test_matching_field_interpolates_and_runs(self):
        wf = _workflow()
        # fix the reference to the real upstream field — run goes green
        wf.nodes["n2"].config.params["issue_number"] = "${result.pr_number}"
        wf.nodes["n2"].config.params["repo"] = "o/r"
        engine = WorkflowEngine()
        execution = await engine.execute(wf)
        assert execution.status == WorkflowStatus.SUCCESS
        n2 = next(s for s in execution.steps if s.node_id == "n2")
        assert n2.status == NodeStatus.SUCCESS
