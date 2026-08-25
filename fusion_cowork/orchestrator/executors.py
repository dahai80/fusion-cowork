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

    def __init__(self):
        # HI-9: 可注入父引擎运行时 (permission/hook/session), 避免裸 WorkflowEngine 绕过权限
        self._permission_manager = None
        self._hook_manager = None
        self._session_store = None

    def inject_runtime(self, permission_manager=None, hook_manager=None, session_store=None) -> None:
        """HI-9: 由 AgentOrchestrator 注入父引擎运行时, 使子工作流受同一权限/ Hook 约束。"""
        self._permission_manager = permission_manager
        self._hook_manager = hook_manager
        self._session_store = session_store

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
            # HI-9: 注入父运行时, 非裸 engine (旧版裸 engine 无 permission → 高危节点不受控)
            engine = WorkflowEngine(
                permission_manager=self._permission_manager,
                hook_manager=self._hook_manager,
                session_store=self._session_store,
            )
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

    # A-3: 默认模型名, list_models 不可达时兜底 (与 agent_loop._resolve_model 一致)
    _FALLBACK_MODEL = "qwen3.5-9b"

    def __init__(self, mode: str = "chat"):
        self._mode = mode

    async def _resolve_model(self, client: Any) -> str:
        model = None
        try:
            models = await client.list_models()
            if models:
                model = models[0].get("id") or models[0].get("model")
        except Exception as e:
            logger.debug(f"MLXExecutor list_models 失败, 用兜底模型: {e}")
        return model or self._FALLBACK_MODEL

    def _build_prompt(self, task_type: str, prompt: str, input_data: Dict[str, Any]) -> str:
        # A-3: classify/summarize 改用单轮 chat (FusionMLXClient 无这两方法), 用 prompt 工程。
        # 原 client.classify/summarize 调用恒 AttributeError → agent 能力不可用。
        if task_type == "classify":
            items = input_data.get("items", [])
            categories = input_data.get("categories", [])
            return (
                f"将下列条目分类到给定类别中, 每条输出一行: <条目> => <类别>。\n"
                f"类别: {', '.join(str(c) for c in categories) if categories else '(自动判断)'}\n"
                f"条目: {items}\n只输出分类结果, 不解释。"
            )
        if task_type == "summarize":
            return f"用中文精简总结以下内容, 不超 200 字:\n\n{prompt}"
        return prompt

    async def __call__(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        prompt = input_data.get("prompt", "")
        task_type = input_data.get("task_type", self._mode)

        if not prompt:
            return {"error": "缺少 prompt 参数"}

        # A-3: with 上下文确保 httpx client 关闭 (A-10: 逐实例泄漏根因)
        try:
            from ..ai import FusionMLXClient, get_budget_tracker

            budget = get_budget_tracker()
            async with FusionMLXClient() as client:
                model = input_data.get("model") or await self._resolve_model(client)
                # A-3: chat(model, messages) — 原 chat(prompt) 缺 model + 传字符串 → 400
                messages = [{"role": "user", "content": self._build_prompt(task_type, prompt, input_data)}]
                result = await client.chat(model, messages)

                usage = getattr(result, "usage", None) or {}
                if usage:
                    ok = budget.record_usage(usage)
                    if not ok:
                        logger.warning(f"MLXExecutor 预算超限, 中止任务: budget={budget.to_dict()}")
                        return {"status": "failed", "error": "token 预算超限", "budget": budget.to_dict()}

                return {
                    "status": "completed",
                    "data": {"content": result.content, "usage": usage, "model": model, "task_type": task_type},
                }
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
            # HI-8: 超时后杀子进程 (旧版 except 不 kill, 进程泄漏)
            if "proc" in locals() and proc.returncode is None:
                proc.kill()
                await proc.wait()
                logger.warning(f"ShellExecutor 超时杀进程: {command[:80]} ({timeout}s)")
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

    # HI-20: 最大委托深度, 防 Coordinator 递归自调用耗尽栈/资源
    MAX_DEPTH = 5

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

        # HI-20: 深度限制 — 子任务继承+1, 超限拒绝 (防 Coordinator→Coordinator 无限递归)
        depth = input_data.get("_depth", 0)
        try:
            depth = int(depth)
        except (TypeError, ValueError):
            depth = 0
        if depth > self.MAX_DEPTH:
            logger.warning(f"CoordinatorExecutor 委托深度超限: depth={depth} > MAX_DEPTH={self.MAX_DEPTH}, 拒绝")
            return {"error": f"委托深度超限 (>{self.MAX_DEPTH}), 已拒绝递归执行"}

        agent_map = {
            "node": "executor_node",
            "workflow": "executor_workflow",
            "ai": "executor_mlx",
            "shell": "executor_shell",
        }
        _agent_id = agent_map.get(subtask_type, "executor_node")

        # HI-20: 子任务 input_data 携带递增 depth, 供下层 Coordinator 继续校验
        child_input = dict(input_data)
        child_input["_depth"] = depth + 1

        task_id = await self._orchestrator.submit_task(
            description=description,
            input_data=child_input,
        )

        # HI-20: parent_task 记录链路 (AgentTask.parent_task 现已设值)
        parent_id = input_data.get("_task_id", "")
        task = self._orchestrator.get_task(task_id)
        if task is not None and parent_id:
            task.parent_task = parent_id

        # R-2: 超时须真取消子任务 handle (旧版只返回 error, 子协程继续跑成僵尸)
        try:
            timeout_sec = float(input_data.get("child_timeout", 30))
        except (TypeError, ValueError):
            timeout_sec = 30.0

        for _ in range(max(1, int(timeout_sec * 2))):
            await asyncio.sleep(0.5)
            task = self._orchestrator.get_task(task_id)
            if task and task.status in ("completed", "failed"):
                return task.output_data if task.status == "completed" else {"error": task.error}

        logger.warning(f"CoordinatorExecutor 子任务超时 ({timeout_sec}s), 取消子任务: {task_id}")
        try:
            await self._orchestrator.cancel_task(task_id)
        except Exception as e:
            logger.error(f"CoordinatorExecutor 取消子任务失败: {e}")
        return {"error": f"子任务超时 ({timeout_sec}s)", "task_id": task_id}
