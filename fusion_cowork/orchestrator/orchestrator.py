"""多智能体联动引擎 — 多个 Agent 协同完成复杂工作流。

V0.3 特性：
- Agent 注册与发现
- 任务分解与分配
- Agent 间通信与协调
- 结果聚合
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class AgentRole(Enum):
    """Agent 角色。"""

    PLANNER = "planner"  # 任务规划
    EXECUTOR = "executor"  # 任务执行
    ANALYZER = "analyzer"  # 结果分析
    VALIDATOR = "validator"  # 结果验证
    COORDINATOR = "coordinator"  # 协调


@dataclass
class Agent:
    """Agent 定义。"""

    agent_id: str
    name: str
    role: AgentRole
    description: str = ""
    capabilities: List[str] = field(default_factory=list)
    endpoint: str = "local"  # local | http://host:port
    status: str = "idle"  # idle | busy | error
    current_task: str = ""

    @property
    def is_local(self) -> bool:
        return self.endpoint == "local"


@dataclass
class AgentTask:
    """Agent 任务定义。"""

    task_id: str
    agent_id: str
    parent_task: str = ""
    description: str = ""
    input_data: Dict[str, Any] = field(default_factory=dict)
    output_data: Dict[str, Any] = field(default_factory=dict)
    status: str = "pending"  # pending | running | completed | failed | cancelled
    created_at: float = 0.0
    started_at: float = 0.0
    completed_at: float = 0.0
    error: str = ""
    # A-9 (audit 0912): acceptance/accountability fields — task-level quality gate
    acceptance_criteria: str = ""
    acceptor: str = ""
    acceptance_status: str = ""  # "" | pending | accepted | rejected
    acceptance_comment: str = ""
    retry_count: int = 0


@dataclass
class OrchestrationPlan:
    """编排计划。"""

    plan_id: str
    workflow_name: str
    tasks: List[AgentTask] = field(default_factory=list)
    dependencies: Dict[str, List[str]] = field(default_factory=dict)
    status: str = "pending"
    created_at: float = 0.0


class AgentOrchestrator:
    """多智能体编排器 — 协调多个 Agent 协同工作。

    支持：
    - Agent 注册与发现
    - 任务分解与分配
    - 依赖关系编排
    - 并行执行
    - 结果聚合
    """

    def __init__(self, permission_manager=None, hook_manager=None, session_store=None):
        self._agents: Dict[str, Agent] = {}
        self._plans: Dict[str, OrchestrationPlan] = {}
        self._executors: Dict[str, Callable] = {}
        self._tasks: Dict[str, AgentTask] = {}
        self._runtimes: Dict[str, Any] = {}
        self._message_bus = None
        # HI-9: 父引擎运行时, 注入子工作流执行器, 使委托工作流受同一权限/ Hook 约束
        self._permission_manager = permission_manager
        self._hook_manager = hook_manager
        self._session_store = session_store
        # HI-7: 保留 asyncio.Task handle, 供 cancel_task 真取消 (而非仅翻 flag)
        self._task_handles: Dict[str, asyncio.Task] = {}
        self._bg_tasks: set = set()
        # HI-8: 单任务超时上限 (秒), execute_plan/_execute_task 用 asyncio.wait_for 包裹
        self._task_timeout: float = 120.0

    def register_default_agents(self) -> None:
        """注册默认 Agent + 执行器。"""
        from .comm import AgentMessageBus
        from .executors import DEFAULT_EXECUTORS, CoordinatorExecutor

        if not self._message_bus:
            self._message_bus = AgentMessageBus()

        default_agents = [
            Agent(
                agent_id="planner",
                name="规划者",
                role=AgentRole.PLANNER,
                description="任务规划与分解",
                capabilities=["plan", "decompose"],
            ),
            Agent(
                agent_id="coordinator",
                name="协调者",
                role=AgentRole.COORDINATOR,
                description="协调子任务分发",
                capabilities=["coordinate", "dispatch"],
            ),
            Agent(
                agent_id="executor_node",
                name="节点执行者",
                role=AgentRole.EXECUTOR,
                description="执行 NodeRegistry 节点",
                capabilities=["node_exec"],
            ),
            Agent(
                agent_id="executor_workflow",
                name="工作流执行者",
                role=AgentRole.EXECUTOR,
                description="执行工作流模板",
                capabilities=["workflow_exec"],
            ),
            Agent(
                agent_id="executor_mlx",
                name="AI 执行者",
                role=AgentRole.EXECUTOR,
                description="调用 MLX AI 服务",
                capabilities=["mlx_chat", "mlx_classify", "mlx_summarize"],
            ),
            Agent(
                agent_id="executor_shell",
                name="命令执行者",
                role=AgentRole.EXECUTOR,
                description="执行 Shell 命令",
                capabilities=["shell_exec"],
            ),
            Agent(
                agent_id="analyzer",
                name="分析者",
                role=AgentRole.ANALYZER,
                description="结果分析与总结",
                capabilities=["analyze", "summarize"],
            ),
            Agent(
                agent_id="validator",
                name="验证者",
                role=AgentRole.VALIDATOR,
                description="结果验证与质量检查",
                capabilities=["validate", "check"],
            ),
        ]

        for agent in default_agents:
            self.register_agent(agent)

        for agent_id, executor in DEFAULT_EXECUTORS.items():
            self.register_executor(agent_id, executor)

        # P0-4 (audit 0912): private ShellExecutor wired to the parent
        # permission runtime — DEFAULT_EXECUTORS is a module-level singleton
        # and must stay ungated for backward compatibility. Registered AFTER
        # the DEFAULT_EXECUTORS loop so it actually wins (ordering bug: the
        # gated executor was previously overwritten by the loop).
        if self._permission_manager is not None:
            from .executors import ShellExecutor

            self.register_executor("executor_shell", ShellExecutor(permission_manager=self._permission_manager))
            logger.debug("orchestrator 已绑定带权限门的私有 ShellExecutor")

        # HI-9: 有父运行时则换私有 WorkflowExecutor (DEFAULT_EXECUTORS 是模块级单例, 共享注入会跨实例污染)
        if self._permission_manager is not None or self._hook_manager is not None or self._session_store is not None:
            from .executors import WorkflowExecutor

            wf_exec = WorkflowExecutor()
            wf_exec.inject_runtime(
                permission_manager=self._permission_manager,
                hook_manager=self._hook_manager,
                session_store=self._session_store,
            )
            self.register_executor("executor_workflow", wf_exec)
            logger.debug("orchestrator 已绑定私有 WorkflowExecutor + 注入父引擎运行时")

        self.register_executor("planner", DEFAULT_EXECUTORS["executor_mlx"])
        self.register_executor("analyzer", DEFAULT_EXECUTORS["executor_mlx"])
        self.register_executor("validator", DEFAULT_EXECUTORS["executor_mlx"])

        self._coordinator_executor = CoordinatorExecutor(self)
        self.register_executor("coordinator", self._coordinator_executor)

        logger.info(f"默认 Agent 注册完成: {len(self._agents)} 个 Agent, {len(self._executors)} 个执行器")

    async def submit_task(self, description: str, input_data: Dict[str, Any] = None) -> str:
        """Submit a task — route to the executor matching the payload shape.

        Routing (A-5, audit 0912): explicit node_name -> executor_node;
        workflow/template_name -> executor_workflow; plain prompt (no command)
        -> coordinator (delegates by subtask_type / falls back to MLX);
        otherwise default to executor_node. Previously everything was pinned
        to executor_node, so natural-language tasks always failed with
        "missing node_name" while still being reported as completed.

        Returns:
            task_id
        """
        input_data = dict(input_data or {})
        if input_data.get("node_name"):
            agent_id = "executor_node"
        elif input_data.get("workflow") or input_data.get("template_name"):
            agent_id = "executor_workflow"
        elif input_data.get("prompt") and not input_data.get("command"):
            agent_id = "coordinator"
        elif input_data.get("command"):
            agent_id = "executor_shell"
        else:
            agent_id = "executor_node"
        task_id = f"task_{uuid.uuid4().hex[:8]}"
        task = AgentTask(
            task_id=task_id,
            agent_id=agent_id,
            description=description,
            # A-9: child tasks carry their own task_id so CoordinatorExecutor
            # can wire parent_task (fixes broken accountability chain).
            input_data=input_data or {"prompt": description},
            created_at=time.time(),
            parent_task=input_data.get("_task_id", ""),
            acceptance_criteria=input_data.get("acceptance_criteria", ""),
            acceptor=input_data.get("acceptor", ""),
        )
        self._tasks[task_id] = task
        # A-9: inject our own id for downstream delegation (parent chain)
        task.input_data.setdefault("_task_id", task_id)

        # 后台执行 — HI-7: 保留 handle 进 _task_handles, 供 cancel_task 真取消协程
        handle = asyncio.create_task(self._run_submitted_task(task))
        self._task_handles[task_id] = handle
        self._bg_tasks.add(handle)
        handle.add_done_callback(lambda h: self._bg_tasks.discard(h))

        return task_id

    async def _run_submitted_task(self, task: AgentTask) -> None:
        """执行提交的任务 — HI-18: CancelledError 单独捕获, finally 置终态 + completed_at。"""
        task.status = "running"
        task.started_at = time.time()

        try:
            executor = self._executors.get(task.agent_id)
            if executor:
                result = executor(task.input_data)
                # A-11 (audit 0912 follow-up): executor may be a callable class
                # instance with an async __call__ (ShellExecutor/NodeExecutor/
                # CoordinatorExecutor...) — iscoroutinefunction() returns False
                # for those, so the returned coroutine was never awaited and
                # the task was marked completed without running anything.
                # Await whatever the executor returned, then apply the timeout.
                if asyncio.iscoroutine(result):
                    result = await asyncio.wait_for(result, timeout=self._task_timeout)
                result = result if isinstance(result, dict) else {"result": result}
                task.output_data = result
                # A-7 (audit 0912): executor-reported failure must map to task
                # failure — previously a node returning {"status": "failed"}
                # still marked the task completed (silent false success).
                _err = result.get("error")
                _failed = result.get("status") in ("failed", "denied", "error") or _err
                if _failed:
                    task.status = "failed"
                    task.error = str(_err or result.get("status") or "executor reported failure")
                else:
                    task.status = "completed"
            else:
                node_executor = self._executors.get("executor_node")
                if node_executor:
                    result = node_executor(task.input_data)
                    if asyncio.iscoroutine(result):
                        result = await asyncio.wait_for(result, timeout=self._task_timeout)
                    result = result if isinstance(result, dict) else {"result": result}
                    task.output_data = result
                    _err = result.get("error")
                    _failed = result.get("status") in ("failed", "denied", "error") or _err
                    if _failed:
                        task.status = "failed"
                        task.error = str(_err or result.get("status") or "executor reported failure")
                    else:
                        task.status = "completed"
                else:
                    task.error = f"无可用执行器: agent_id={task.agent_id}"
                    task.output_data = {"status": "no_executor", "agent_id": task.agent_id, "input": task.input_data}
                    task.status = "failed"
                    logger.error(f"任务无执行器且无降级路径: {task.task_id} agent_id={task.agent_id}")
        except asyncio.CancelledError:
            # HI-18: CancelledError 是 BaseException, 旧 except Exception 不接 → status 卡 running
            # P2-3 (audit 0912): don't downgrade an already-terminal status set
            # by a racing cancel_task()/completion — first terminal write wins.
            if task.status not in ("completed", "failed", "cancelled"):
                task.status = "cancelled"
                task.error = "用户取消"
            logger.info(f"提交任务被取消: {task.task_id}")
            raise
        except TimeoutError:
            # R-2: wait_for 超时 → executor 协程已取消, 置终态避免卡 running
            task.status = "failed"
            task.error = f"任务超时 ({self._task_timeout}s)"
            logger.warning(f"提交任务超时: {task.task_id} ({self._task_timeout}s)")
        except Exception as e:
            task.error = str(e)
            task.status = "failed"
            logger.error(f"提交任务执行异常: {e}")
        finally:
            task.completed_at = time.time()
            self._task_handles.pop(task.task_id, None)
            # R-1: 终态任务从 _tasks 剔除, 防 _tasks 无界增长 (运行态保留供查询)
            if task.status in ("completed", "failed", "cancelled"):
                self._tasks.pop(task.task_id, None)

    def cancel_task(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if not task:
            logger.warning(f"取消任务失败: 任务不存在 {task_id}")
            return False
        if task.status in ("completed", "failed", "cancelled"):
            logger.info(f"任务已终态，不可取消: {task_id} status={task.status}")
            return False
        # HI-8: 真取消运行协程, 非仅翻 flag (flip flag 不停已运行 executor)
        handle = self._task_handles.get(task_id)
        if handle is not None and not handle.done():
            handle.cancel()
            logger.info(f"任务协程已请求取消: {task_id}")
        task.status = "cancelled"
        task.error = "用户取消"
        task.completed_at = time.time()
        logger.info(f"任务已取消: {task_id}")
        return True

    def register_agent(self, agent: Agent) -> None:
        """注册 Agent。"""
        self._agents[agent.agent_id] = agent
        logger.info(f"Agent 注册: {agent.name} ({agent.role.value})")

    def unregister_agent(self, agent_id: str) -> None:
        """注销 Agent。"""
        self._agents.pop(agent_id, None)
        logger.info(f"Agent 注销: {agent_id}")

    def register_executor(self, agent_id: str, executor: Callable) -> None:
        """注册 Agent 执行器。"""
        self._executors[agent_id] = executor

    def get_agents_by_role(self, role: AgentRole) -> List[Agent]:
        """按角色获取 Agent。"""
        return [a for a in self._agents.values() if a.role == role]

    def has_agent(self, agent_id: str) -> bool:
        """A-10 (audit 0912): public membership check — avoids external access
        to the private _agents dict (e.g. SpaceAgentRuntime.register_to_orchestrator)."""
        return agent_id in self._agents

    # ── A-9: acceptance gate (audit 0912) ──

    def request_acceptance(self, task_id: str, acceptor: str = "") -> Dict[str, Any]:
        """Mark a completed task as pending acceptance (quality gate)."""
        task = self._tasks.get(task_id)
        if not task:
            return {"error": f"任务不存在: {task_id}"}
        if task.status != "completed":
            return {"error": f"任务未完成，不可验收: status={task.status}"}
        task.acceptance_status = "pending"
        if acceptor:
            task.acceptor = acceptor
        return {"task_id": task_id, "acceptance_status": "pending", "acceptor": task.acceptor}

    def accept_task(self, task_id: str, verdict: str, comment: str = "", acceptor: str = "") -> Dict[str, Any]:
        """Record an acceptance verdict for a task.

        verdict: "accepted" -> task is confirmed done;
                 "rejected" -> task reopens (status=pending, retry_count+1) for rework.
        """
        task = self._tasks.get(task_id)
        if not task:
            return {"error": f"任务不存在: {task_id}"}
        if verdict not in ("accepted", "rejected"):
            return {"error": f"非法验收结论: {verdict} (accepted|rejected)"}
        if acceptor:
            task.acceptor = acceptor
        task.acceptance_status = verdict
        task.acceptance_comment = comment
        if verdict == "accepted":
            logger.info(f"任务验收通过: {task_id} acceptor={task.acceptor}")
            return {"task_id": task_id, "acceptance_status": "accepted", "status": task.status}
        # rejected -> reopen for rework
        task.status = "pending"
        task.retry_count += 1
        task.completed_at = 0.0
        logger.info(f"任务验收驳回，重开返工: {task_id} retry={task.retry_count}")
        return {"task_id": task_id, "acceptance_status": "rejected", "status": task.status, "retry_count": task.retry_count}

    def reopen_task(self, task_id: str) -> bool:
        """Re-run a rejected/pending task through its executor in the background."""
        task = self._tasks.get(task_id)
        if not task or task.status not in ("pending", "failed"):
            return False
        handle = asyncio.create_task(self._run_submitted_task(task))
        self._task_handles[task.task_id] = handle
        self._bg_tasks.add(handle)
        handle.add_done_callback(lambda h: self._bg_tasks.discard(h))
        return True

    # ── 编排计划 ──

    async def create_plan(
        self,
        workflow_name: str,
        description: str,
    ) -> OrchestrationPlan:
        """创建编排计划。"""
        plan = OrchestrationPlan(
            plan_id=f"plan_{uuid.uuid4().hex[:8]}",
            workflow_name=workflow_name,
            created_at=time.time(),
        )
        self._plans[plan.plan_id] = plan
        logger.info(f"编排计划创建: {plan.plan_id} ({workflow_name})")
        return plan

    def add_task(
        self,
        plan_id: str,
        agent_id: str,
        description: str,
        input_data: Dict[str, Any] = None,
        depends_on: List[str] = None,
    ) -> Optional[AgentTask]:
        """向计划添加任务。"""
        plan = self._plans.get(plan_id)
        if not plan:
            logger.error(f"计划不存在: {plan_id}")
            return None

        task = AgentTask(
            task_id=f"task_{uuid.uuid4().hex[:8]}",
            agent_id=agent_id,
            description=description,
            input_data=input_data or {},
            created_at=time.time(),
        )
        plan.tasks.append(task)

        if depends_on:
            plan.dependencies[task.task_id] = depends_on

        logger.info(f"任务添加: {task.task_id} → {agent_id}")
        return task

    async def execute_plan(self, plan_id: str) -> Dict[str, Any]:
        """Execute an orchestration plan.

        A-4 (audit 0912): dependency readiness now requires upstream SUCCESS —
        failed/skipped prerequisites no longer silently feed downstream tasks.
        Plan terminal status reflects reality: completed | partial | failed.
        """
        plan = self._plans.get(plan_id)
        if not plan:
            return {"error": f"计划不存在: {plan_id}"}

        plan.status = "running"
        results = {}
        start_time = time.time()

        def _failed(task_id: str) -> bool:
            r = results.get(task_id)
            if not isinstance(r, dict):
                return False
            return bool(r.get("error")) or r.get("status") in ("failed", "denied", "error")

        # 拓扑排序执行
        executed = set()
        while len(executed) < len(plan.tasks):
            # 找出可执行的任务 — prerequisites must have executed successfully
            ready = []
            for task in plan.tasks:
                if task.task_id in executed:
                    continue
                deps = plan.dependencies.get(task.task_id, [])
                if all(d in executed and not _failed(d) for d in deps):
                    ready.append(task)

            if not ready:
                # 死锁检测 — distinguish "upstream failed" from a real cycle
                remaining = [t.task_id for t in plan.tasks if t.task_id not in executed]
                blocked = {
                    t.task_id: [d for d in plan.dependencies.get(t.task_id, []) if _failed(d)]
                    for t in plan.tasks
                    if t.task_id not in executed
                }
                blocked_by_failure = {tid: deps for tid, deps in blocked.items() if deps}
                if blocked_by_failure:
                    for tid, deps in blocked_by_failure.items():
                        t = next((x for x in plan.tasks if x.task_id == tid), None)
                        if t is not None:
                            t.status = "skipped"
                            t.error = f"skipped: upstream failed {deps}"
                            results[tid] = {"status": "skipped", "error": t.error}
                            executed.add(tid)
                    # remaining tasks may now be ready — re-loop
                    if all(t.task_id in executed for t in plan.tasks):
                        break
                    still_ready = [
                        t
                        for t in plan.tasks
                        if t.task_id not in executed
                        and all(d in executed and not _failed(d) for d in plan.dependencies.get(t.task_id, []))
                    ]
                    if still_ready:
                        ready = still_ready
                    else:
                        remaining = [t.task_id for t in plan.tasks if t.task_id not in executed]
                        logger.error(f"任务死锁: {remaining}")
                        plan.status = "failed"
                        return {"error": f"任务死锁: {remaining}", "results": results}
                else:
                    logger.error(f"任务死锁: {remaining}")
                    plan.status = "failed"
                    return {"error": f"任务死锁: {remaining}", "results": results}

            # 并行执行就绪任务
            # A-2: aggregate-mode tasks (analyzer) receive real upstream results
            # instead of the bare task-id list.
            for task in ready:
                agg_ids = task.input_data.get("_aggregate_task_ids")
                if agg_ids:
                    aggregated = {
                        tid: (results.get(tid) if isinstance(results.get(tid), dict) else {"result": results.get(tid)})
                        for tid in agg_ids
                        if tid in results
                    }
                    task.input_data = {
                        "prompt": f"Summarize the following {len(aggregated)} subtask results.",
                        "results": aggregated,
                    }
            tasks = [self._execute_task(task, plan) for task in ready]
            task_results = await asyncio.gather(*tasks, return_exceptions=True)

            for task, result in zip(ready, task_results):
                executed.add(task.task_id)
                if isinstance(result, Exception):
                    results[task.task_id] = {"error": str(result)}
                    task.status = "failed"
                    task.error = str(result)
                elif isinstance(result, dict) and (result.get("error") or result.get("status") in ("failed", "denied", "error")):
                    # A-4: executor-reported failure is a failed task (was "completed")
                    results[task.task_id] = result
                    task.status = "failed"
                    task.error = str(result.get("error") or result.get("status"))
                    task.completed_at = time.time()
                else:
                    results[task.task_id] = result
                    task.status = "completed"
                    task.output_data = result or {}
                    task.completed_at = time.time()

        # A-4: honest plan terminal status — completed | partial | failed
        failed_tasks = [t for t in plan.tasks if t.status in ("failed", "skipped")]
        ok_tasks = [t for t in plan.tasks if t.status == "completed"]
        if not ok_tasks and failed_tasks:
            plan.status = "failed"
        elif failed_tasks:
            plan.status = "partial"
        else:
            plan.status = "completed"
        elapsed = time.time() - start_time
        logger.info(f"编排完成: {plan_id} ({elapsed:.2f}s) status={plan.status}")

        return {
            "plan_id": plan_id,
            "status": plan.status,
            "elapsed": elapsed,
            "results": results,
        }

    async def _execute_task(
        self,
        task: AgentTask,
        plan: OrchestrationPlan,
    ) -> Dict[str, Any]:
        """执行单个任务。"""
        agent = self._agents.get(task.agent_id)
        if not agent:
            return {"error": f"Agent 不存在: {task.agent_id}"}

        task.status = "running"
        task.started_at = time.time()
        executor = self._executors.get(task.agent_id)

        if executor:
            try:
                result = executor(task.input_data)
                # A-11 (audit 0912 follow-up): await coroutine results — see
                # _run_submitted_task for the callable-class-instance rationale.
                if asyncio.iscoroutine(result):
                    # HI-8: 单任务超时, 防卡死 executor 拖垮整个 plan
                    result = await asyncio.wait_for(result, timeout=self._task_timeout)
                return result if isinstance(result, dict) else {"result": result}
            except TimeoutError:
                task.status = "failed"
                task.error = f"任务超时 ({self._task_timeout}s)"
                logger.warning(f"任务超时: {task.task_id} ({self._task_timeout}s)")
                return {"error": task.error}
            except Exception as e:
                task.status = "failed"
                task.error = str(e)
                return {"error": str(e)}
        else:
            # 无执行器时尝试默认执行器
            from .executors import DEFAULT_EXECUTORS

            fallback = DEFAULT_EXECUTORS.get("executor_node")
            if fallback:
                try:
                    result = fallback(task.input_data)
                    # A-11: same awaitable-result handling as the primary path
                    if asyncio.iscoroutine(result):
                        result = await asyncio.wait_for(result, timeout=self._task_timeout)
                    return result if isinstance(result, dict) else {"result": result}
                except TimeoutError:
                    task.status = "failed"
                    task.error = f"降级任务超时 ({self._task_timeout}s)"
                    logger.warning(f"降级任务超时: {task.task_id} ({self._task_timeout}s)")
                    return {"error": task.error}
                except Exception as e:
                    task.status = "failed"
                    task.error = str(e)
                    return {"error": str(e)}
            # 兜底: 标记为无执行器
            logger.warning(f"Agent {task.agent_id} 无执行器，跳过")
            await asyncio.sleep(0.1)
            return {"status": "no_executor", "input": task.input_data}

    # ── 编排模板 ──

    async def run_standard_pipeline(
        self,
        input_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Run the standard pipeline: real plan-then-execute decomposition.

        A-2 (audit 0912): the Planner now actually runs first and its JSON
        output is parsed into subtasks (description / agent_id / input_data /
        depends_on / acceptance_criteria) that are added to the plan with
        real dependencies. Previously the same input was copied verbatim to
        the planner and three executors — the plan output was never read, so
        "decomposition" was pure theater.
        """
        import re

        planner = self.get_agents_by_role(AgentRole.PLANNER)
        if not planner:
            return {"error": "无 Planner Agent"}

        plan = await self.create_plan("standard_pipeline", "标准编排流水线")

        # 1. Run the Planner first, alone.
        plan_task = self.add_task(
            plan.plan_id,
            planner[0].agent_id,
            "任务规划",
            {
                "prompt": (
                    "Break the following task into subtasks. Reply with ONLY a JSON array, "
                    "each item: {\"description\": str, \"agent_id\": one of "
                    "[executor_node, executor_workflow, executor_mlx, executor_shell], "
                    "\"input_data\": object, \"depends_on\": [subtask indexes], "
                    "\"acceptance_criteria\": str}.\nTASK:\n"
                    + json.dumps(input_data, ensure_ascii=False, default=str)
                )
            },
        )
        stage = await self.execute_plan(plan.plan_id)
        plan_output = stage.get("results", {}).get(plan_task.task_id, {})
        content = ""
        if isinstance(plan_output, dict):
            data = plan_output.get("data")
            if isinstance(data, dict):
                content = str(data.get("content", ""))
            else:
                content = str(plan_output.get("result", "") or "")
        subtasks = []
        try:
            match = re.search(r"\[.*\]", content, re.DOTALL)
            if match:
                parsed = json.loads(match.group(0))
                if isinstance(parsed, list):
                    subtasks = parsed
        except (json.JSONDecodeError, TypeError):
            subtasks = []

        if not subtasks:
            # Fail loudly instead of silently faking a pipeline (A-2).
            plan.status = "failed"
            return {
                "error": "Planner 未产出可解析的任务拆解",
                "plan_id": plan.plan_id,
                "raw_planner_output": content[:500],
            }

        # 2. Materialize the parsed subtasks as real plan tasks.
        id_by_index: Dict[int, str] = {}
        for i, st in enumerate(subtasks):
            if not isinstance(st, dict):
                continue
            deps = []
            for d in st.get("depends_on", []):
                try:
                    dep_id = id_by_index[int(d)]
                except (KeyError, TypeError, ValueError):
                    continue
                deps.append(dep_id)
            t = self.add_task(
                plan.plan_id,
                str(st.get("agent_id", "executor_node")),
                str(st.get("description", f"subtask {i + 1}")),
                dict(st.get("input_data") or {}),
                depends_on=deps or None,
            )
            if t is not None:
                t.acceptance_criteria = str(st.get("acceptance_criteria", ""))
                t.acceptance_status = "pending" if t.acceptance_criteria else ""
                id_by_index[i] = t.task_id

        # 3. Analyzer summarizes executor outputs once all subtasks finish.
        analyzers = self.get_agents_by_role(AgentRole.ANALYZER)
        if analyzers:
            executor_tasks = [t.task_id for t in plan.tasks if t.agent_id != planner[0].agent_id]
            if executor_tasks:
                self.add_task(
                    plan.plan_id,
                    analyzers[0].agent_id,
                    "结果分析",
                    {"_aggregate_task_ids": executor_tasks},
                    depends_on=executor_tasks,
                )

        return await self.execute_plan(plan.plan_id)

    def get_plan_status(self, plan_id: str) -> Optional[Dict[str, Any]]:
        """获取计划状态。"""
        plan = self._plans.get(plan_id)
        if not plan:
            return None
        return {
            "plan_id": plan.plan_id,
            "workflow_name": plan.workflow_name,
            "status": plan.status,
            "total_tasks": len(plan.tasks),
            "completed": sum(1 for t in plan.tasks if t.status == "completed"),
            "failed": sum(1 for t in plan.tasks if t.status == "failed"),
            "running": sum(1 for t in plan.tasks if t.status == "running"),
        }

    # ── AgentRuntime 生命周期 ──

    async def start_runtimes(self) -> None:
        """启动所有已注册 Agent 的 Runtime。"""
        from .agent_runtime import AgentRuntime

        if not self._message_bus:
            from .comm import AgentMessageBus

            self._message_bus = AgentMessageBus()

        for agent_id, agent in self._agents.items():
            executor = self._executors.get(agent_id)
            if executor and agent_id not in self._runtimes:
                runtime = AgentRuntime(agent, executor, self._message_bus)
                await runtime.start()
                self._runtimes[agent_id] = runtime

        logger.info(f"AgentRuntime 启动完成: {len(self._runtimes)} 个运行时")

    async def stop_runtimes(self) -> None:
        """停止所有 Runtime — HI-7/18: 同时取消残留后台任务, 防泄漏。"""
        for runtime in self._runtimes.values():
            await runtime.stop()
        self._runtimes.clear()
        for handle in list(self._bg_tasks):
            if not handle.done():
                handle.cancel()
        if self._bg_tasks:
            await asyncio.gather(*self._bg_tasks, return_exceptions=True)
            self._bg_tasks.clear()
        self._task_handles.clear()
        # R-1: 停机清终态残留, 防 _tasks/_plans 跨生命周期累积
        self._tasks.clear()
        self._plans.clear()
        logger.info("所有 AgentRuntime 已停止")

    def get_message_bus(self):
        """获取消息总线。"""
        return self._message_bus

    def get_task(self, task_id: str):
        """获取任务状态（公共 API，避免外部访问 _tasks）。"""
        return self._tasks.get(task_id)
