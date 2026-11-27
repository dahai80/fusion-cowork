"""Agent executors — actually execute agent tasks.

One executor per role:
- NodeExecutor: executes NodeRegistry nodes (executor role)
- WorkflowExecutor: executes workflow templates (executor role)
- MLXExecutor: calls the fusion-mlx AI service (analyzer/validator role)
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)


class NodeExecutor:
    """Node executor — creates and executes nodes via NodeRegistry."""

    async def __call__(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        node_name = input_data.get("node_name", "")
        # planner contract: params live at input_data TOP level (the schema
        # validator checks them there); "node_params" stays supported as an
        # explicit nested override for callers that use it.
        node_params = input_data.get("node_params")
        if not isinstance(node_params, dict) or not node_params:
            node_params = {k: v for k, v in input_data.items() if k != "node_name"}

        if not node_name:
            return {"error": "缺少 node_name 参数"}  # i18n: user-facing

        try:
            from ..engine.node import NodeConfig, NodeRegistry

            node = NodeRegistry.create(node_name, config=NodeConfig(params=node_params))
            if not node:
                return {"error": f"节点创建失败: {node_name}"}  # i18n: user-facing

            result = await node.execute(node_params)
            return {
                "status": result.status.value,
                "data": result.data,
                "summary": result.summary,
                "error": result.error,
            }
        except Exception as e:
            logger.error(f"NodeExecutor error: {e}")
            return {"error": str(e)}


class WorkflowExecutor:
    """Workflow executor — executes workflows via WorkflowEngine."""

    def __init__(self):
        # HI-9: parent engine runtime injectable (permission/hook/session), avoids a bare WorkflowEngine bypassing permissions
        self._permission_manager = None
        self._hook_manager = None
        self._session_store = None

    def inject_runtime(self, permission_manager=None, hook_manager=None, session_store=None) -> None:
        """HI-9: parent engine runtime injected by AgentOrchestrator, so sub-workflows are bound by the same permission/hook rules."""
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
                    return {"error": f"模板不存在: {template_name}"}  # i18n: user-facing

            if not workflow_def:
                return {"error": "缺少 workflow 或 template_name 参数"}  # i18n: user-facing

            wf = Workflow.from_dict(workflow_def)
            # HI-9: inject the parent runtime, not a bare engine (old bare engine had no permission -> high-risk nodes uncontrolled)
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
            logger.error(f"WorkflowExecutor error: {e}")
            return {"error": str(e)}


class MLXExecutor:
    """MLX executor — calls the fusion-mlx AI service."""

    # A-3: default model name, fallback when list_models is unreachable (consistent with agent_loop._resolve_model)
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
            logger.debug(f"MLXExecutor list_models failed, using fallback model: {e}")
        return model or self._FALLBACK_MODEL

    def _build_prompt(self, task_type: str, prompt: str, input_data: Dict[str, Any]) -> str:
        # A-3: classify/summarize now use single-turn chat (FusionMLXClient has no such methods), via prompt engineering.
        # The old client.classify/summarize calls always raised AttributeError -> agent capabilities unusable.
        if task_type == "classify":
            items = input_data.get("items", [])
            categories = input_data.get("categories", [])
            return (
                f"将下列条目分类到给定类别中, 每条输出一行: <条目> => <类别>。\n"  # i18n: user-facing
                f"类别: {', '.join(str(c) for c in categories) if categories else '(自动判断)'}\n"  # i18n: user-facing
                f"条目: {items}\n只输出分类结果, 不解释。"  # i18n: user-facing
            )
        if task_type == "summarize":
            return f"用中文精简总结以下内容, 不超 200 字:\n\n{prompt}"  # i18n: user-facing
        return prompt

    async def __call__(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        prompt = input_data.get("prompt", "")
        task_type = input_data.get("task_type", self._mode)

        if not prompt:
            return {"error": "缺少 prompt 参数"}  # i18n: user-facing

        # A-3: with-context ensures the httpx client closes (A-10: per-instance leak root cause)
        try:
            from ..ai import FusionMLXClient, get_budget_tracker

            budget = get_budget_tracker()
            async with FusionMLXClient() as client:
                model = input_data.get("model") or await self._resolve_model(client)
                # A-3: chat(model, messages) — the old chat(prompt) lacked model + passed a string -> 400
                messages = [{"role": "user", "content": self._build_prompt(task_type, prompt, input_data)}]
                result = await client.chat(model, messages)

                usage = getattr(result, "usage", None) or {}
                if usage:
                    ok = budget.record_usage(usage)
                    if not ok:
                        logger.warning(f"MLXExecutor budget exceeded, aborting task: budget={budget.to_dict()}")
                        return {
                            "status": "failed",
                            "error": "token 预算超限",  # i18n: user-facing
                            "budget": budget.to_dict(),
                        }  # i18n: user-facing

                return {
                    "status": "completed",
                    "data": {"content": result.content, "usage": usage, "model": model, "task_type": task_type},
                }
        except Exception as e:
            logger.error(f"MLXExecutor error: {e}")
            return {"error": str(e)}


class ShellExecutor:
    """Shell executor — executes command lines."""

    def __init__(self, permission_manager=None):
        # P0-4 (audit 0912): permission gate — the orchestration path executed
        # arbitrary shell commands with no PermissionManager/guard check, while
        # the same shell_exec node inside the engine required three-level
        # approval. Wire the gate here so shell delegation is subject to the
        # same deny rules / HIGH_RISK_NODES / guard verdict as the engine.
        self._permission_manager = permission_manager

    async def __call__(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        command = input_data.get("command", "")
        timeout = input_data.get("timeout", 60)

        if not command:
            return {"error": "缺少 command 参数"}  # i18n: user-facing

        # P0-4: check permission before spawning any process
        if self._permission_manager is not None:
            try:
                allowed = await self._permission_manager.check("shell_exec", "execute", {"command": command})
            except Exception as e:
                logger.error(f"ShellExecutor permission check error, fail-closed: {e}")
                return {"status": "denied", "error": f"shell 命令权限检查异常: {e}"}  # i18n: user-facing
            if not allowed:
                logger.warning(f"ShellExecutor command denied by permission system: {command[:80]}")
                return {"status": "denied", "error": "shell 命令被权限系统拒绝, 需人工审批"}  # i18n: user-facing

        try:
            proc = None
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
            # HI-8: kill the subprocess after timeout (old version did not kill in except, leaking processes)
            if proc is not None and proc.returncode is None:
                proc.kill()
                await proc.wait()
                logger.warning(f"ShellExecutor killed process on timeout: {command[:80]} ({timeout}s)")
            return {"error": f"命令超时 ({timeout}s)"}  # i18n: user-facing
        except Exception as e:
            logger.error(f"ShellExecutor error: {e}")
            return {"error": str(e)}


DEFAULT_EXECUTORS = {
    "executor_node": NodeExecutor(),
    "executor_workflow": WorkflowExecutor(),
    "executor_mlx": MLXExecutor(),
    "executor_shell": ShellExecutor(),
}


class CoordinatorExecutor:
    """Coordinator executor — decomposes large tasks to matching sub-agents."""

    # HI-20: max delegation depth, prevents Coordinator recursive self-calls exhausting stack/resources
    MAX_DEPTH = 5

    def __init__(self, orchestrator=None):
        self._orchestrator = orchestrator

    def bind(self, orchestrator) -> None:
        self._orchestrator = orchestrator

    async def __call__(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        if not self._orchestrator:
            return {"error": "未绑定 AgentOrchestrator"}  # i18n: user-facing

        description = input_data.get("prompt", input_data.get("description", ""))
        subtask_type = input_data.get("subtask_type", "node")

        if not description:
            return {"error": "缺少 prompt 或 description 参数"}  # i18n: user-facing

        # HI-20: depth limit — subtask inherits +1, reject over the limit (prevents Coordinator->Coordinator infinite recursion)
        depth = input_data.get("_depth", 0)
        try:
            depth = int(depth)
        except (TypeError, ValueError):
            depth = 0
        if depth > self.MAX_DEPTH:
            logger.warning(
                f"CoordinatorExecutor delegation depth exceeded: depth={depth} > MAX_DEPTH={self.MAX_DEPTH}, refusing"
            )
            return {"error": f"委托深度超限 (>{self.MAX_DEPTH}), 已拒绝递归执行"}  # i18n: user-facing

        agent_map = {
            "node": "executor_node",
            "workflow": "executor_workflow",
            "ai": "executor_mlx",
            "shell": "executor_shell",
        }
        _agent_id = agent_map.get(subtask_type, "executor_node")

        # HI-20: subtask input_data carries the incremented depth so lower Coordinators keep validating
        child_input = dict(input_data)
        child_input["_depth"] = depth + 1

        task_id = await self._orchestrator.submit_task(
            description=description,
            input_data=child_input,
        )

        # HI-20: parent_task records the chain (AgentTask.parent_task is now set)
        parent_id = input_data.get("_task_id", "")
        task = self._orchestrator.get_task(task_id)
        if task is not None and parent_id:
            task.parent_task = parent_id

        # R-2: on timeout truly cancel the subtask handle (old version only returned an error while the sub-coroutine kept running as a zombie)
        try:
            timeout_sec = float(input_data.get("child_timeout", 30))
        except (TypeError, ValueError):
            timeout_sec = 30.0

        # P2-2 (audit 0912): fixed-interval polling replaced with an
        # asyncio.Event signaled by the child's done-callback — the parent
        # wakes the moment the child reaches a terminal state instead of
        # waking every 0.5 s (busy-wait + up to 0.5 s added latency).
        done_event = asyncio.Event()

        def _signal(_t: asyncio.Task) -> None:
            done_event.set()

        # Fast path: the child may already be terminal (or get_task is mocked
        # in tests) — don't wait for an event that will never fire.
        task = self._orchestrator.get_task(task_id)
        if task is not None and task.status in ("completed", "failed", "cancelled"):
            return task.output_data if task.status == "completed" else {"error": task.error}

        child_handle = self._orchestrator._task_handles.get(task_id)
        if isinstance(child_handle, asyncio.Task):
            child_handle.add_done_callback(_signal)

        try:
            await asyncio.wait_for(done_event.wait(), timeout=timeout_sec)
        except TimeoutError:
            logger.warning(f"CoordinatorExecutor subtask timed out ({timeout_sec}s), cancelling subtask: {task_id}")
            try:
                # cancel_task is a sync API on AgentOrchestrator
                self._orchestrator.cancel_task(task_id)
            except Exception as e:
                logger.error(f"CoordinatorExecutor failed to cancel subtask: {e}")
            return {"error": f"子任务超时 ({timeout_sec}s)", "task_id": task_id}  # i18n: user-facing
        finally:
            if isinstance(child_handle, asyncio.Task):
                try:
                    child_handle.remove_done_callback(_signal)
                except Exception:
                    pass

        task = self._orchestrator.get_task(task_id)
        if task is not None and task.status == "completed":
            return task.output_data
        return {"error": (task.error if task is not None else "子任务终态异常")}  # i18n: user-facing
