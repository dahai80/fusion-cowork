"""issue #462: sub_workflow 节点 — 子 DAG 作为一等节点执行。

嵌套深度上限、失败/超时传播、参数继承 (params.input)、别名注册。
"""

from __future__ import annotations

import asyncio

from fusion_cowork import NODE_NAME_ALIASES
from fusion_cowork.engine import PermissionManager, WorkflowEngine
from fusion_cowork.engine.node import BaseNode, NodeCategory, NodeRegistry, NodeResult, NodeStatus, register_node
from fusion_cowork.engine.workflow import Workflow
from fusion_cowork.nodes import import_all_nodes


@register_node
class _Echo460(BaseNode):
    name = "echo460"
    display_name = "Echo 460"
    category = NodeCategory.LOGIC
    description = "test probe: passes seed through (#462)"
    icon = "🧪"
    default_label = "Echo"

    async def execute(self, inputs: dict) -> NodeResult:
        return NodeResult(status=NodeStatus.SUCCESS, data={"seed": str(inputs.get("seed", ""))}, summary="echo")


@register_node
class _Probe460(BaseNode):
    name = "probe460"
    display_name = "Probe 460"
    category = NodeCategory.LOGIC
    description = "test probe: exposes session id from inputs (#462)"
    icon = "🧪"
    default_label = "Probe"

    async def execute(self, inputs: dict) -> NodeResult:
        return NodeResult(status=NodeStatus.SUCCESS, data={"sid": str(inputs.get("_session_id", ""))}, summary="probe")


@register_node
class _Fail460(BaseNode):
    name = "fail460"
    display_name = "Fail 460"
    category = NodeCategory.LOGIC
    description = "test probe: always fails (#462)"
    icon = "🧪"
    default_label = "Fail"

    async def execute(self, inputs: dict) -> NodeResult:
        return NodeResult(status=NodeStatus.FAILED, error="fail460 有意失败", summary="fail")


@register_node
class _Slow461(BaseNode):
    name = "slow461"
    display_name = "Slow 461"
    category = NodeCategory.LOGIC
    description = "test probe: sleeping node (#462)"
    icon = "🧪"
    default_label = "Slow"

    async def execute(self, inputs: dict) -> NodeResult:
        await asyncio.sleep(0.4)
        return NodeResult(status=NodeStatus.SUCCESS, data={"done": True}, summary="slow")


def _setup():
    import_all_nodes()
    NodeRegistry.register_alias("子工作流", "sub_workflow")
    NodeRegistry.register_alias("工作流嵌套", "sub_workflow")


def _engine():
    return WorkflowEngine(permission_manager=PermissionManager())


def _child_def(node="echo460"):
    return {"nodes": [{"id": "c1", "name": node, "config": {}}], "edges": []}


def _parent_wf(sub_params):
    return Workflow.from_dict(
        {
            "name": "parent-wf",
            "nodes": [{"id": "sub1", "name": "sub_workflow", "config": {"params": sub_params}}],
            "edges": [],
        }
    )


class TestSubWorkflowNode:
    def test_registered_with_alias(self):
        _setup()
        assert NodeRegistry.get("sub_workflow") is not None
        assert NODE_NAME_ALIASES.get("子工作流") == "sub_workflow"
        assert NODE_NAME_ALIASES.get("工作流嵌套") == "sub_workflow"

    async def test_child_executes_and_returns_outputs(self):
        _setup()
        eng = _engine()
        exec_result = await eng.execute(
            _parent_wf({"workflow": _child_def("echo460"), "input": {"seed": "x"}}),
            initial_input={},
        )
        assert exec_result.status.value == "success", exec_result.error
        step = exec_result.steps[0]
        assert step.status.value == "success"
        data = step.output_data
        assert data["status"] == "success"
        # 参数继承: params.input 注入子工作流首节点
        assert data["outputs"]["seed"] == "x", data
        assert data["steps"][0]["node_id"] == "c1"
        assert data["steps"][0]["session_id"] is None or isinstance(data["steps"][0]["session_id"], str)

    async def test_child_failure_propagates(self):
        _setup()
        eng = _engine()
        exec_result = await eng.execute(_parent_wf({"workflow": _child_def("fail460")}), initial_input={})
        assert exec_result.status.value == "failed"
        assert "fail460" in (exec_result.error or "") or "fail460" in str(exec_result.steps[0].error)

    async def test_depth_cap_enforced(self):
        _setup()
        eng = _engine()
        # 子 DAG 内再嵌一层 sub_workflow, depth_cap=1 → 内层在 depth=1 被拒
        nested = {
            "nodes": [
                {
                    "id": "inner",
                    "name": "sub_workflow",
                    "config": {"params": {"workflow": _child_def("echo460"), "depth_cap": 1}},
                }
            ],
            "edges": [],
        }
        exec_result = await eng.execute(
            _parent_wf({"workflow": nested, "depth_cap": 5}),
            initial_input={},
        )
        assert exec_result.status.value == "failed"
        assert "嵌套超限" in (exec_result.error or ""), exec_result.error

    async def test_depth_cap_success_at_boundary(self):
        _setup()
        eng = _engine()
        nested = {
            "nodes": [
                {
                    "id": "inner",
                    "name": "sub_workflow",
                    "config": {"params": {"workflow": _child_def("echo460"), "depth_cap": 2}},
                }
            ],
            "edges": [],
        }
        exec_result = await eng.execute(
            _parent_wf({"workflow": nested, "depth_cap": 5}),
            initial_input={},
        )
        assert exec_result.status.value == "success", exec_result.error

    async def test_outside_engine_fails_visibly(self):
        _setup()
        from fusion_cowork.nodes.ecosystem.subworkflow_node import SubWorkflowNode

        node = SubWorkflowNode()
        node.config.params = {"workflow": _child_def()}
        res = await node.execute({})
        assert res.status.value == "failed"
        assert "引擎" in res.error

    async def test_invalid_def_fails(self):
        _setup()
        eng = _engine()
        exec_result = await eng.execute(_parent_wf({"workflow": {"nodes": []}}), initial_input={})
        assert exec_result.status.value == "failed"
        assert "workflow" in (exec_result.error or "")

    async def test_child_timeout_propagates(self):
        _setup()
        eng = _engine()
        exec_result = await eng.execute(
            _parent_wf({"workflow": _child_def("slow461"), "timeout": 0.05}),
            initial_input={},
        )
        assert exec_result.status.value == "failed"
        assert "超时" in (exec_result.error or ""), exec_result.error
