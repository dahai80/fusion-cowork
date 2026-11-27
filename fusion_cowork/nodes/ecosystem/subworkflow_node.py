"""SubWorkflow node (issue #462) — executes a child DAG as a first-class node.

Base primitive of the template layering: the child workflow runs in-process
under a full child engine (wave scheduling / HITL / permission config shared
with the parent engine), param inheritance via params.input, outputs returned
in node data. Depth cap guards against recursion blowup; child failure/timeout
propagates as node FAILED (never silently).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict

from ...engine.node import BaseNode, NodeCategory, NodeResult, NodeStatus, register_node
from ...engine.workflow import Workflow, _current_engine

logger = logging.getLogger(__name__)

_DEFAULT_DEPTH_CAP = 5
_DEFAULT_TIMEOUT = 600.0


@register_node
class SubWorkflowNode(BaseNode):
    """Sub-workflow node — executes an embedded child DAG as a single node."""

    name = "sub_workflow"
    display_name = "子工作流"  # i18n: user-facing
    category = NodeCategory.FUSION_ECOSYSTEM
    description = "Execute an embedded child workflow DAG as a single node"
    icon = "🪆"
    default_label = "子工作流"  # i18n: user-facing

    inputs = [
        # i18n: user-facing
        {"key": "workflow", "label": "子工作流定义", "type": "object"},
        # i18n: user-facing
        {"key": "input", "label": "子工作流初始输入", "type": "object"},
    ]
    outputs = [
        # i18n: user-facing
        {"key": "status", "label": "子工作流终态", "type": "string"},
        # i18n: user-facing
        {"key": "result_summary", "label": "结果摘要", "type": "string"},
        # i18n: user-facing
        {"key": "outputs", "label": "子工作流末节点输出", "type": "object"},
        # i18n: user-facing
        {"key": "steps", "label": "子步骤列表", "type": "list"},
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                # i18n: user-facing
                "workflow": {"type": "object", "description": "子工作流定义 ({nodes, edges})"},
                # i18n: user-facing
                "input": {"type": "object", "description": "子工作流初始输入 (参数继承)", "default": {}},
                # i18n: user-facing
                "depth_cap": {"type": "number", "description": "嵌套深度上限", "default": _DEFAULT_DEPTH_CAP},
                "timeout": {
                    "type": "number",
                    # i18n: user-facing
                    "description": "子工作流超时秒数 (0 = 不限)",
                    "default": _DEFAULT_TIMEOUT,
                },
            },
            "required": ["workflow"],
        }

    async def execute(self, inputs: dict) -> NodeResult:
        params = getattr(self.config, "params", {}) or {}
        wf_def = params.get("workflow") or inputs.get("workflow")
        if not isinstance(wf_def, dict) or not wf_def.get("nodes"):
            return NodeResult(
                status=NodeStatus.FAILED, error="sub_workflow 参数 workflow 必填 ({nodes, edges})"  # i18n: user-facing
            )  # i18n: user-facing
        parent_engine = _current_engine.get()
        if parent_engine is None:
            return NodeResult(
                status=NodeStatus.FAILED, error="sub_workflow 只能在引擎执行的节点内使用"  # i18n: user-facing
            )  # i18n: user-facing
        depth = int(inputs.get("_subworkflow_depth", 0) or 0)
        try:
            depth_cap = int(params.get("depth_cap", _DEFAULT_DEPTH_CAP))
        except (TypeError, ValueError):
            depth_cap = _DEFAULT_DEPTH_CAP
        if depth >= depth_cap:
            return NodeResult(
                status=NodeStatus.FAILED,
                # i18n: user-facing
                error=f"子工作流嵌套超限: depth={depth} >= depth_cap={depth_cap}",
            )
        try:
            child_wf = Workflow.from_dict(wf_def)
        except ValueError as e:
            return NodeResult(status=NodeStatus.FAILED, error=f"子工作流定义无效: {e}")  # i18n: user-facing
        child_input = dict(params.get("input") or {})
        child_input["_subworkflow_depth"] = depth + 1
        # issue #462: child engine shares the parent engine's permission/HITL/session config — consistent governance semantics
        from ...engine import WorkflowEngine

        child_engine = WorkflowEngine(
            permission_manager=getattr(parent_engine, "_permission_manager", None),
            session_store=getattr(parent_engine, "_session_store", None),
        )
        try:
            timeout = float(params.get("timeout", _DEFAULT_TIMEOUT) or 0)
        except (TypeError, ValueError):
            timeout = _DEFAULT_TIMEOUT
        logger.info(
            f"sub_workflow start: parent={child_input.get('_subworkflow_depth', 1)} depth={depth + 1}/{depth_cap} "
            f"nodes={len(child_wf.nodes)}"
        )
        try:
            if timeout > 0:
                execution = await asyncio.wait_for(
                    child_engine.execute(child_wf, initial_input=child_input), timeout=timeout
                )
            else:
                execution = await child_engine.execute(child_wf, initial_input=child_input)
        except TimeoutError:
            return NodeResult(status=NodeStatus.FAILED, error=f"子工作流执行超时 ({timeout:g}s)")  # i18n: user-facing
        except ValueError as e:
            return NodeResult(status=NodeStatus.FAILED, error=f"子工作流定义无效: {e}")  # i18n: user-facing
        status = execution.status.value if hasattr(execution.status, "value") else str(execution.status)
        if status != "success":
            return NodeResult(
                status=NodeStatus.FAILED,
                # i18n: user-facing
                error=f"子工作流终态 {status}: {execution.error or '无错误详情'}",
                data={"child_status": status, "result_summary": execution.result_summary or ""},
            )
        outputs = execution.steps[-1].output_data if execution.steps else {}
        steps_trimmed = [
            {
                "node_id": s.node_id,
                "node_name": s.node_name,
                "status": s.status.value if hasattr(s.status, "value") else str(s.status),
                "execution_time": s.execution_time,
                "summary": s.summary,
                "session_id": s.session_id,
            }
            for s in execution.steps
        ]
        logger.info(f"sub_workflow done: status={status} steps={len(steps_trimmed)} time={execution.total_time:.2f}s")
        return NodeResult(
            status=NodeStatus.SUCCESS,
            data={
                "status": status,
                "result_summary": execution.result_summary or "",
                "outputs": dict(outputs) if isinstance(outputs, dict) else {},
                "steps": steps_trimmed,
                "total_time": execution.total_time,
            },
            # i18n: user-facing
            summary=execution.result_summary or "子工作流执行完成",
        )
