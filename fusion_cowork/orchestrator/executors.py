"""Agent 执行器 — 真实执行 Agent 任务。

每个角色对应一个执行器:
- NodeExecutor: 执行 NodeRegistry 节点 (executor 角色)
- WorkflowExecutor: 执行工作流模板 (executor 角色)
- MLXExecutor: 调用 fusion-mlx AI 服务 (analyzer/validator 角色)
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)


class NodeExecutor:
    """节点执行器 — 通过 NodeRegistry 创建并执行节点。"""

    async def __call__(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        node_name = input_data.get("node_name", "")
        node_params = input_data.get("node_params", {})

        if not node_name:
            return {"error": "缺少 node_name 参数"}

        try:
            from ..engine.node import NodeConfig, NodeRegistry
            node = NodeRegistry.create(node_name, config=NodeConfig(params=node_params))
            if not node:
                return {"error": f"节点创建失败: {node_name}"}

            result = await node.execute(node_params)
            return {
                "status": result.status.value,
                "data": result.data,
                "summary": result.summary,
                "error": result.error,
            }
        except Exception as e:
            logger.error(f"NodeExecutor 异常: {e}")
            return {"error": str(e)}


class WorkflowExecutor:
    """工作流执行器 — 通过 WorkflowEngine 执行工作流。"""

    async def __call__(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        workflow_def = input_data.get("workflow", {})
        template_name = input_data.get("template_name", "")

        try:
            from ..engine import Workflow, WorkflowEngine
            from ..templates import TemplateManager

            if template_name:
                mgr = TemplateManager()
                workflow_def = mgr.get_template(template_name)
                if not workflow_def:
                    return {"error": f"模板不存在: {template_name}"}

            if not workflow_def:
                return {"error": "缺少 workflow 或 template_name 参数"}

            wf = Workflow.from_dict(workflow_def)
            engine = WorkflowEngine()
            result = await engine.execute(wf)
            return {
                "status": result.status.value,
                "data": result.data,
                "summary": result.summary,
            }
        except Exception as e:
            logger.error(f"WorkflowExecutor 异常: {e}")
            return {"error": str(e)}


class MLXExecutor:
    """MLX 执行器 — 调用 fusion-mlx AI 服务。"""

    def __init__(self, mode: str = "chat"):
        self._mode = mode

    async def __call__(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        prompt = input_data.get("prompt", "")
        task_type = input_data.get("task_type", self._mode)

        if not prompt:
            return {"error": "缺少 prompt 参数"}

        try:
            from ..ai import FusionMLXClient
            client = FusionMLXClient()

            if task_type == "chat":
                result = await client.chat(prompt)
            elif task_type == "classify":
                result = await client.classify(
                    input_data.get("items", []),
                    input_data.get("categories", []),
                )
            elif task_type == "summarize":
                result = await client.summarize(prompt)
            else:
                result = await client.chat(prompt)

            return {"status": "completed", "data": result}
        except Exception as e:
            logger.error(f"MLXExecutor 异常: {e}")
            return {"error": str(e)}


class ShellExecutor:
    """Shell 执行器 — 执行命令行。"""

    async def __call__(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        command = input_data.get("command", "")
        timeout = input_data.get("timeout", 60)

        if not command:
            return {"error": "缺少 command 参数"}

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return {
                "status": "completed" if proc.returncode == 0 else "failed",
                "returncode": proc.returncode,
                "stdout": stdout.decode("utf-8", errors="replace")[-2000:],
                "stderr": stderr.decode("utf-8", errors="replace")[-1000:],
            }
        except TimeoutError:
            return {"error": f"命令超时 ({timeout}s)"}
        except Exception as e:
            logger.error(f"ShellExecutor 异常: {e}")
            return {"error": str(e)}


DEFAULT_EXECUTORS = {
    "executor_node": NodeExecutor(),
    "executor_workflow": WorkflowExecutor(),
    "executor_mlx": MLXExecutor(),
    "executor_shell": ShellExecutor(),
}


class CoordinatorExecutor:
    """协调执行器 — 将大任务分解给匹配的子 Agent 执行。"""

    def __init__(self, orchestrator=None):
        self._orchestrator = orchestrator

    def bind(self, orchestrator) -> None:
        self._orchestrator = orchestrator

    async def __call__(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        if not self._orchestrator:
            return {"error": "未绑定 AgentOrchestrator"}

        description = input_data.get("prompt", input_data.get("description", ""))
        subtask_type = input_data.get("subtask_type", "node")

        if not description:
            return {"error": "缺少 prompt 或 description 参数"}

        agent_map = {
            "node": "executor_node",
            "workflow": "executor_workflow",
            "ai": "executor_mlx",
            "shell": "executor_shell",
        }
        _agent_id = agent_map.get(subtask_type, "executor_node")

        task_id = await self._orchestrator.submit_task(
            description=description,
            input_data=input_data,
        )

        for _ in range(60):
            await asyncio.sleep(0.5)
            task = self._orchestrator.get_task(task_id)
            if task and task.status in ("completed", "failed"):
                return task.output_data if task.status == "completed" else {"error": task.error}

        return {"error": "子任务超时 (30s)", "task_id": task_id}
