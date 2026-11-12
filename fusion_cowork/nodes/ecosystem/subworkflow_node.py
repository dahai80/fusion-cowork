"""SubWorkflow node (issue #462) — 把子 DAG 作为一等节点执行。

模板分层的基础原语: 子工作流在本进程内由子引擎完整执行 (波次调度/HITL/
权限配置与父引擎共享), 参数继承经 params.input, 输出回传节点 data。
深度上限防递归爆炸; 子工作流失败/超时以节点 FAILED 传播 (不静默)。
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
    """子工作流节点 — 内嵌子 DAG 作为单节点执行。"""

    name = "sub_workflow"
    display_name = "子工作流"
    category = NodeCategory.FUSION_ECOSYSTEM
    description = "Execute an embedded child workflow DAG as a single node"
    icon = "🪆"
    default_label = "子工作流"

    inputs = [
        {"key": "workflow", "label": "子工作流定义", "type": "object"},
        {"key": "input", "label": "子工作流初始输入", "type": "object"},
    ]
    outputs = [
        {"key": "status", "label": "子工作流终态", "type": "string"},
        {"key": "result_summary", "label": "结果摘要", "type": "string"},
        {"key": "outputs", "label": "子工作流末节点输出", "type": "object"},
        {"key": "steps", "label": "子步骤列表", "type": "list"},
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "workflow": {"type": "object", "description": "子工作流定义 ({nodes, edges})"},
                "input": {"type": "object", "description": "子工作流初始输入 (参数继承)", "default": {}},
                "depth_cap": {"type": "number", "description": "嵌套深度上限", "default": _DEFAULT_DEPTH_CAP},
                "timeout": {
                    "type": "number",
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
            return NodeResult(status=NodeStatus.FAILED, error="sub_workflow 参数 workflow 必填 ({nodes, edges})")
        parent_engine = _current_engine.get()
        if parent_engine is None:
            return NodeResult(status=NodeStatus.FAILED, error="sub_workflow 只能在引擎执行的节点内使用")
        depth = int(inputs.get("_subworkflow_depth", 0) or 0)
        try:
            depth_cap = int(params.get("depth_cap", _DEFAULT_DEPTH_CAP))
        except (TypeError, ValueError):
            depth_cap = _DEFAULT_DEPTH_CAP
        if depth >= depth_cap:
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"子工作流嵌套超限: depth={depth} >= depth_cap={depth_cap}",
            )
        try:
            child_wf = Workflow.from_dict(wf_def)
        except ValueError as e:
            return NodeResult(status=NodeStatus.FAILED, error=f"子工作流定义无效: {e}")
        child_input = dict(params.get("input") or {})
        child_input["_subworkflow_depth"] = depth + 1
        # issue #462: 子引擎共享父引擎权限/HITL/会话配置 — 治理语义一致
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
            f"sub_workflow 开始: parent={child_input.get('_subworkflow_depth', 1)} depth={depth + 1}/{depth_cap} "
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
            return NodeResult(status=NodeStatus.FAILED, error=f"子工作流执行超时 ({timeout:g}s)")
        except ValueError as e:
            return NodeResult(status=NodeStatus.FAILED, error=f"子工作流定义无效: {e}")
        status = execution.status.value if hasattr(execution.status, "value") else str(execution.status)
        if status != "success":
            return NodeResult(
                status=NodeStatus.FAILED,
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
        logger.info(f"sub_workflow 完成: status={status} steps={len(steps_trimmed)} time={execution.total_time:.2f}s")
        return NodeResult(
            status=NodeStatus.SUCCESS,
            data={
                "status": status,
                "result_summary": execution.result_summary or "",
                "outputs": dict(outputs) if isinstance(outputs, dict) else {},
                "steps": steps_trimmed,
                "total_time": execution.total_time,
            },
            summary=execution.result_summary or "子工作流执行完成",
        )
