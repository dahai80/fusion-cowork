"""Agent executors — actually execute agent tasks.

One executor per role:
- NodeExecutor: runs NodeRegistry nodes (executor role)
- WorkflowExecutor: runs workflow templates (executor role)
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
            return {"error": "missing node_name parameter"}

        try:
            from ..engine.node import NodeConfig, NodeRegistry

            node = NodeRegistry.create(node_name, config=NodeConfig(params=node_params))
            if not node:
                return {"error": f"node creation failed: {node_name}"}

            result = await node.execute(node_params)
            return {
                "status": result.status.value,
                "data": result.data,
                "summary": result.summary,
                "error": result.error,
            }
        except Exception as e:
            logger.error(f"NodeExecutor exception: {e}")
            return {"error": str(e)}


class WorkflowExecutor:
    """Workflow executor — executes workflows via WorkflowEngine."""

    def __init__(self):
        # HI-9: injectable parent engine runtime (permission/hook/session), prevents a bare WorkflowEngine from bypassing permissions
        self._permission_manager = None
        self._hook_manager = None
        self._session_store = None

    def inject_runtime(self, permission_manager=None, hook_manager=None, session_store=None) -> None:
        """HI-9: AgentOrchestrator injects the parent engine runtime so sub-workflows are bound by the same permission/hook rules."""
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
                    return {"error": f"template not found: {template_name}"}

            if not workflow_def:
                return {"error": "missing workflow or template_name parameter"}

            wf = Workflow.from_dict(workflow_def)
            # HI-9: inject the parent runtime, not a bare engine (old bare engine had no permission → high-risk nodes uncontrolled)
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
            logger.error(f"WorkflowExecutor exception: {e}")
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
        # A-3: classify/summarize switched to single-turn chat (FusionMLXClient has no such methods), via prompt engineering.
        # The old client.classify/summarize calls always raised AttributeError → the agent capability was unusable.
        if task_type == "classify":
            items = input_data.get("items", [])
            categories = input_data.get("categories", [])
            return (
                f"Classify the following items into the given categories, one line per item: <item> => <category>.\n"
                f"Categories: {', '.join(str(c) for c in categories) if categories else '(auto-detect)'}\n"
                f"Items: {items}\nOutput only the classification result, no explanation."
            )
        if task_type == "summarize":
            return f"Summarize the following content concisely in Chinese, within 200 characters:\n\n{prompt}"
        return prompt

    async def __call__(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        prompt = input_data.get("prompt", "")
        task_type = input_data.get("task_type", self._mode)

        if not prompt:
            return {"error": "missing prompt parameter"}

        # A-3: with-context ensures the httpx client is closed (A-10: root cause of the per-instance leak)
        try:
            from ..ai import FusionMLXClient, get_budget_tracker

            budget = get_budget_tracker()
            async with FusionMLXClient() as client:
                model = input_data.get("model") or await self._resolve_model(client)
                # A-3: chat(model, messages) — the old chat(prompt) lacked model and passed a string → 400
                messages = [{"role": "user", "content": self._build_prompt(task_type, prompt, input_data)}]
                result = await client.chat(model, messages)

                usage = getattr(result, "usage", None) or {}
                if usage:
                    ok = budget.record_usage(usage)
                    if not ok:
                        logger.warning(f"MLXExecutor budget exceeded, aborting task: budget={budget.to_dict()}")
                        return {"status": "failed", "error": "token budget exceeded", "budget": budget.to_dict()}

                return {
                    "status": "completed",
                    "data": {"content": result.content, "usage": usage, "model": model, "task_type": task_type},
                }
        except Exception as e:
            logger.error(f"MLXExecutor exception: {e}")
            return {"error": str(e)}


class ShellExecutor:
    """Shell executor — runs command lines."""

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
            return {"error": "missing command parameter"}

        # P0-4: check permission before spawning any process
        if self._permission_manager is not None:
            try:
                allowed = await self._permission_manager.check("shell_exec", "execute", {"command": command})
            except Exception as e:
                logger.error(f"ShellExecutor permission check exception, fail-closed: {e}")
                return {"status": "denied", "error": f"shell command permission check exception: {e}"}
            if not allowed:
                logger.warning(f"ShellExecutor command denied by permission system: {command[:80]}")
                return {"status": "denied", "error": "shell command denied by permission system, manual approval required"}

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
            # HI-8: kill the child process after timeout (old except did not kill, leaking processes)
            if proc is not None and proc.returncode is None:
                proc.kill()
                await proc.wait()
                logger.warning(f"ShellExecutor killed process on timeout: {command[:80]} ({timeout}s)")
            return {"error": f"command timed out ({timeout}s)"}
        except Exception as e:
            logger.error(f"ShellExecutor exception: {e}")
            return {"error": str(e)}


DEFAULT_EXECUTORS = {
    "executor_node": NodeExecutor(),
    "executor_workflow": WorkflowExecutor(),
    "executor_mlx": MLXExecutor(),
    "executor_shell": ShellExecutor(),
}


class CoordinatorExecutor:
    """Coordinator executor — decomposes large tasks and delegates them to matching sub-agents."""

    # HI-20: max delegation depth, prevents Coordinator self-recursion from exhausting stack/resources
    MAX_DEPTH = 5

    def __init__(self, orchestrator=None):
        self._orchestrator = orchestrator

    def bind(self, orchestrator) -> None:
        self._orchestrator = orchestrator

    async def __call__(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        if not self._orchestrator:
            return {"error": "no AgentOrchestrator bound"}

        description = input_data.get("prompt", input_data.get("description", ""))
        subtask_type = input_data.get("subtask_type", "node")

        if not description:
            return {"error": "missing prompt or description parameter"}

        # HI-20: depth limit — subtask inherits +1, rejected when over the cap (prevents Coordinator→Coordinator infinite recursion)
        depth = input_data.get("_depth", 0)
        try:
            depth = int(depth)
        except (TypeError, ValueError):
            depth = 0
        if depth > self.MAX_DEPTH:
            logger.warning(f"CoordinatorExecutor delegation depth exceeded: depth={depth} > MAX_DEPTH={self.MAX_DEPTH}, rejected")
            return {"error": f"delegation depth exceeded (>{self.MAX_DEPTH}), recursive execution refused"}

        agent_map = {
            "node": "executor_node",
            "workflow": "executor_workflow",
            "ai": "executor_mlx",
            "shell": "executor_shell",
        }
        _agent_id = agent_map.get(subtask_type, "executor_node")

        # HI-20: subtask input_data carries an incremented depth so lower Coordinators keep validating
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

        # R-2: on timeout the child task handle must be truly cancelled (old version just returned an error, the child coroutine kept running as a zombie)
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
            return {"error": f"subtask timed out ({timeout_sec}s)", "task_id": task_id}
        finally:
            if isinstance(child_handle, asyncio.Task):
                try:
                    child_handle.remove_done_callback(_signal)
                except Exception:
                    pass

        task = self._orchestrator.get_task(task_id)
        if task is not None and task.status == "completed":
            return task.output_data
        return {"error": (task.error if task is not None else "subtask terminal state error")}
