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
import os
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# v2 方案一: single source of truth for executor-reported failure — no_executor
# (the no-executor fallback) previously fell through as success (P1-2).
FAILED_STATUSES = {"failed", "denied", "error", "no_executor"}


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
    # 方案一 (audit v3): presence heartbeat — monotonic-ish wall clock of the
    # last liveness write; dashboard marks running agents stale when
    # now - last_seen exceeds HEARTBEAT_STALE_SECONDS.
    last_seen: float = 0.0

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
    # 方案五 (audit v3): delivery-note field — every superseded predecessor
    # (planner corrective retry) recorded so the retrospective can explain
    # WHY the delivered plan looks the way it does.
    superseded_history: List[Dict[str, Any]] = field(default_factory=list)


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
        # audit E2E (0913): R-1 pops terminal tasks from _tasks, which made
        # accept_task/reopen_task return 任务不存在 for every finished task —
        # the acceptance gate could never fire on the submit path. Keep a
        # bounded archive so terminal tasks stay verifiable/reopenable.
        self._task_archive: OrderedDict[str, AgentTask] = OrderedDict()

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

    async def _presence_heartbeat(self, agent: Agent) -> None:
        """方案一 (audit v3): refresh agent.last_seen every 30s while a task
        runs. Cancelled by the task's finally block; a hung executor stops
        refreshing, so the dashboard can mark the agent stale."""
        try:
            while True:
                await asyncio.sleep(30)
                agent.last_seen = time.time()
        except asyncio.CancelledError:
            pass

    async def _run_submitted_task(self, task: AgentTask) -> None:
        """执行提交的任务 — HI-18: CancelledError 单独捕获, finally 置终态 + completed_at。"""
        task.status = "running"
        task.started_at = time.time()
        # v2 方案二 (P1-3): presence must reflect the submit path too — it was
        # only maintained by AgentRuntime (message-bus), so the dashboard
        # showed every agent idle while orchestrated work was running.
        agent = self._agents.get(task.agent_id)
        if agent is not None:
            agent.status = "busy"
            agent.current_task = task.task_id
            agent.last_seen = time.time()
        # 方案一 (audit v3): heartbeat while the task runs — a hung executor
        # (process alive, task stuck) must become visible on the dashboard
        # instead of showing "busy" forever.
        heartbeat = asyncio.create_task(self._presence_heartbeat(agent)) if agent is not None else None
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
                _failed = result.get("status") in FAILED_STATUSES or _err
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
                    _failed = result.get("status") in FAILED_STATUSES or _err
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
            if heartbeat is not None:
                heartbeat.cancel()
            if agent is not None:
                agent.status = "idle"
                agent.current_task = ""
                agent.last_seen = time.time()
            # R-1: 终态任务从 _tasks 剔除, 防 _tasks 无界增长 (运行态保留供查询)
            if task.status in ("completed", "failed", "cancelled"):
                self._tasks.pop(task.task_id, None)
                self._archive_task(task)

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
        task = self.get_task(task_id)
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
        task = self.get_task(task_id)
        if not task:
            return {"error": f"任务不存在: {task_id}"}
        if verdict not in ("accepted", "rejected"):
            return {"error": f"非法验收结论: {verdict} (accepted|rejected)"}
        if acceptor:
            task.acceptor = acceptor
        task.acceptance_status = verdict
        task.acceptance_comment = comment
        # v2 方案三 (P2): acceptance verdicts were only in-memory — the
        # retrospective pool could not answer "which tasks did a human
        # accept/reject". Append a durable trajectory event (best-effort).
        try:
            from ..trajectory.recorder import TrajectoryEvent, TrajectoryWriter

            TrajectoryWriter().write(
                TrajectoryEvent(
                    ts=time.time(),
                    event="task_acceptance",
                    execution_id=task_id,
                    workflow_id=task_id,
                    workflow_name=f"acceptance:{verdict}",
                    status=verdict,
                    is_error=verdict == "rejected",
                    data={
                        "task_id": task_id,
                        "agent_id": task.agent_id,
                        "verdict": verdict,
                        "comment": comment[:300],
                        "acceptor": task.acceptor,
                        "retry_count": task.retry_count,
                        "task_status": task.status,
                    },
                )
            )
        except Exception as e:
            logger.debug(f"acceptance trajectory skipped: {e}")
        if verdict == "accepted":
            logger.info(f"任务验收通过: {task_id} acceptor={task.acceptor}")
            return {"task_id": task_id, "acceptance_status": "accepted", "status": task.status}
        # rejected -> reopen for rework
        task.status = "pending"
        task.retry_count += 1
        task.completed_at = 0.0
        # v2 P2: reset started_at too — dashboard elapsed = completed_at -
        # started_at; a stale started_at made rework rows show nonsense times.
        task.started_at = 0.0
        logger.info(f"任务验收驳回，重开返工: {task_id} retry={task.retry_count}")
        return {
            "task_id": task_id,
            "acceptance_status": "rejected",
            "status": task.status,
            "retry_count": task.retry_count,
        }

    def reopen_task(self, task_id: str) -> bool:
        """Re-run a rejected/pending task through its executor in the background."""
        task = self.get_task(task_id)
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
            return bool(r.get("error")) or r.get("status") in FAILED_STATUSES

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
                elif isinstance(result, dict) and (result.get("error") or result.get("status") in FAILED_STATUSES):
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

        # Retrospective (audit 方案二③): plan terminal snapshot → trajectory
        # jsonl, so every plan's task tree + failures + acceptance state is
        # reviewable afterwards (zero new storage: reuses TrajectoryWriter).
        self._write_plan_retrospective(plan, results, elapsed)

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
        # v2 方案二 (P1-3): same presence sync as the submit path — plan-path
        # subtasks previously left agents showing idle on the dashboard.
        agent = self._agents.get(task.agent_id)
        heartbeat = None
        if agent is not None:
            agent.status = "busy"
            agent.current_task = task.task_id
            agent.last_seen = time.time()
            # 方案一 (audit v3): same heartbeat as the submit path.
            heartbeat = asyncio.create_task(self._presence_heartbeat(agent))

        try:
            if executor is not None:
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
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
            if agent is not None:
                agent.status = "idle"
                agent.current_task = ""
                agent.last_seen = time.time()

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
        #    Feed the planner the real node registry so subtasks reference
        #    executable node names instead of free-form prose (audit E2E:
        #    without the catalog a small model produced input_data without
        #    node_name and every subtask failed with 缺少 node_name 参数).
        try:
            from fusion_cowork.nodes import import_all_nodes

            import_all_nodes()  # idempotent; headless callers may not have registered nodes yet
        except Exception:
            pass
        try:
            from fusion_cowork.engine.node import NodeRegistry
        except Exception:
            NodeRegistry = None
        # v2 P2+ (27B drill follow-up): derive per-node REQUIRED params (with
        # types) from NodeRegistry params_schema — the planner previously got
        # bare node names only, so it could not know e.g. file_find needs
        # search_path and invented or omitted params at execution time.
        node_required: Dict[str, list] = {}
        entries = []
        if NodeRegistry is not None:
            try:
                entries = NodeRegistry.list()
            except Exception:
                entries = []
        node_lines: list = []
        for n in sorted(entries, key=lambda x: x.get("name", "")):
            name = n.get("name", "")
            schema = n.get("params_schema") or {}
            required = [str(r) for r in (schema.get("required") or [])]
            if name:
                node_required[name] = required
            if not required:
                continue
            props = schema.get("properties") or {}
            parts = []
            for r in required:
                ptype = str((props.get(r) or {}).get("type", "any"))
                parts.append(f"{r} ({ptype})")
            node_lines.append(f"- {name}: required params: {', '.join(parts)}")
        node_catalog = (
            "Available nodes for executor_node input_data.node_name (pick ONLY from these), "
            "with their REQUIRED params — every listed param MUST be present in input_data:\n"
            + "\n".join(node_lines)
            + "\n"
            if node_lines
            else ""
        )
        # v2 方案五: business-role catalog — the planner may assign either an
        # executor id or a business role id; roles carry duties/deliverables/
        # default acceptor and map to executors at materialization.
        try:
            from .role_registry import get_role_registry

            role_catalog = (
                "Business roles (agent_id may be a role id; it maps to its executor "
                "and its default acceptor is auto-bound):\n" + get_role_registry().prompt_catalog() + "\n"
            )
            valid_role_ids = set(get_role_registry().role_ids())
        except Exception:
            role_catalog = ""
            valid_role_ids = set()
        planner_prompt = (
            "Break the following task into subtasks. Reply with ONLY a JSON array, "
            'each item: {"description": str, '
            '"agent_id": executor id (executor_node/executor_workflow/executor_mlx/executor_shell) '
            "OR a business role id from the role catalog below, "
            '"input_data": OBJECT (never a string; for executor_node it MUST include '
            '"node_name" taken from the catalog below, plus that node\'s required params; '
            'for executor_shell it MUST include "command"; '
            'for executor_mlx it MUST include "prompt" (the instruction for the model); '
            "depends_on indexes MUST NOT form cycles and MUST only reference earlier items), "
            '"depends_on": [subtask indexes], '
            '"acceptance_criteria": str}.\n'
            + role_catalog
            + node_catalog
            # v2+: few-shot anchor — quantized models imitate the shown shape
            # far more reliably than they follow prose rules, so pin the
            # contract with one compliant sample (indexes strictly increasing,
            # every required param present, object input_data).
            + "EXAMPLE of a compliant reply (imitate this shape EXACTLY):\n"
            + "[\n"
            + '  {"description": "统计 src 目录的 py 文件数", "agent_id": "executor_shell", '
            + '"input_data": {"command": "find src -name \\"*.py\\" | wc -l", "timeout": 30}, '
            + '"depends_on": [], "acceptance_criteria": "stdout 为非负整数"},\n'
            + '  {"description": "汇总结果", "agent_id": "executor_mlx", '
            + '"input_data": {"prompt": "总结上一条命令的输出并给出结论"}, '
            + '"depends_on": [0], "acceptance_criteria": "结论明确引用数字"}\n'
            + "]\n"
            + "TASK:\n"
            + json.dumps(input_data, ensure_ascii=False, default=str)
        )
        # Schema-validate the planner output; on violation, retry with
        # corrective feedback (audit 方案三: small models routinely emit
        # string input_data / invented node names / cyclic depends_on).
        # v2+: up to 3 attempts with ACCUMULATED violations — each round's
        # feedback carries every distinct violation seen so far, so the model
        # cannot repeat an earlier mistake unnoticed.
        valid_nodes = set(node_required)
        subtasks: list = []
        last_content = ""
        problems: list = []
        all_problems: list = []
        for attempt in range(3):
            if attempt > 0:
                # v2 方案一 (P1-1): retry on a FRESH plan — reusing the old plan
                # re-ran the failed planner task inside it, so a fully-successful
                # retry still reported plan.status="partial" (honest-terminal
                # mechanism sabotaged by its own retry). Supersede + recreate.
                plan.status = "superseded"
                # 方案五 (audit v3): record WHY this predecessor was dropped —
                # the accumulated schema violations of the attempt it carried.
                plan.superseded_history.append(
                    {
                        "superseded_at": time.time(),
                        "attempt": attempt,
                        "violations": list(all_problems[:12]),
                    }
                )
                logger.info(f"plan {plan.plan_id} superseded by planner retry")
                # v4 方案③: drop the superseded plan from _plans — the dict
                # was runtime-unbounded and every retry added one more entry.
                # The violation history transfers to the successor plan (its
                # final retrospective carries it), so no delivery-note data
                # is lost by dropping the dead object.
                superseded_history = plan.superseded_history
                self._plans.pop(plan.plan_id, None)
                plan = await self.create_plan("standard_pipeline_retry", "标准编排流水线(重试)")
                plan.superseded_history = superseded_history
            # 方案四 (audit v3): planner model routing — the planner's
            # decomposition quality drives the whole pipeline, so allow a
            # dedicated (stronger) model for it alone via FUSION_PLANNER_MODEL;
            # executors keep using the default resolution (FUSION_MLX_MODEL /
            # registry). MLXExecutor honors input_data["model"] as override.
            planner_input: Dict[str, Any] = {"prompt": planner_prompt}
            planner_model = os.environ.get("FUSION_PLANNER_MODEL", "").strip()
            if planner_model:
                planner_input["model"] = planner_model
            plan_task = self.add_task(
                plan.plan_id,
                planner[0].agent_id,
                "任务规划" if attempt == 0 else f"任务规划(重试{attempt})",
                planner_input,
            )
            stage = await self.execute_plan(plan.plan_id)
            plan_output = stage.get("results", {}).get(plan_task.task_id, {})
            content = ""
            if isinstance(plan_output, dict):
                data = plan_output.get("data")
                if isinstance(data, dict):
                    content = str(data.get("content", ""))
                else:
                    # executor payloads vary: {"content": ...} (coordinator/mlx)
                    # or {"result": ...} — accept both shapes
                    content = str(plan_output.get("content", "") or plan_output.get("result", "") or "")
            last_content = content
            parsed = None
            try:
                match = re.search(r"\[.*\]", content, re.DOTALL)
                if match:
                    candidate = json.loads(match.group(0))
                    if isinstance(candidate, list):
                        parsed = candidate
            except (json.JSONDecodeError, TypeError):
                parsed = None
            problems = self._planner_schema_problems(parsed, valid_nodes, valid_role_ids, node_required)
            if not problems:
                subtasks = parsed or []
                break
            # v2+: accumulate DISTINCT violations across rounds so each retry's
            # feedback repeats every earlier mistake — the model cannot silently
            # re-violate a rule it already broke in a previous attempt.
            for pr in problems:
                if pr not in all_problems:
                    all_problems.append(pr)
            if attempt < 2:
                logger.warning(f"Planner 输出未通过 schema 校验 (attempt {attempt + 1}/3), 纠错重试: {problems}")
                planner_prompt = (
                    planner_prompt
                    + "\n\nYour previous reply was INVALID for these reasons:\n- "
                    + "\n- ".join(all_problems[:12])
                    + "\nFix ALL of them and reply again with ONLY the corrected JSON array."
                )
        if not subtasks:
            # Fail loudly instead of silently faking a pipeline (A-2).
            plan.status = "failed"
            return {
                "error": "Planner 未产出可解析的任务拆解" + (f" (schema: {problems})" if problems else ""),
                "plan_id": plan.plan_id,
                "raw_planner_output": last_content[:500],
            }

        # 2. Materialize the parsed subtasks as real plan tasks.
        # v2 方案五: business role ids resolve to their executor here, and the
        # role's default acceptor is auto-bound (accountability: every task
        # has a responsible acceptor even if the model omitted one).
        id_by_index: Dict[int, str] = {}
        try:
            from .role_registry import get_role_registry

            role_reg = get_role_registry()
        except Exception:
            role_reg = None
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
            raw_agent = str(st.get("agent_id", "executor_node"))
            role = role_reg.get(raw_agent) if role_reg else None
            executor = role.executor if role else raw_agent
            t = self.add_task(
                plan.plan_id,
                executor,
                str(st.get("description", f"subtask {i + 1}")),
                st["input_data"]
                if isinstance(st.get("input_data"), dict)
                else {"prompt": str(st.get("input_data") or "")},
                depends_on=deps or None,
            )
            if t is not None:
                t.acceptance_criteria = str(st.get("acceptance_criteria", ""))
                t.acceptance_status = "pending" if t.acceptance_criteria else ""
                # 方案五: auto-bind the role's default acceptor; fall back to
                # the coordinator so the task never has a blank responsible party.
                t.acceptor = role.acceptor if (role and role.acceptor) else "coordinator"
                if role:
                    t.input_data.setdefault("_business_role", role.role_id)
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

    @staticmethod
    def _planner_schema_problems(
        parsed,
        valid_nodes: set,
        valid_role_ids: Optional[set] = None,
        node_required: Optional[Dict[str, list]] = None,
    ) -> list:
        """Validate a parsed planner subtask array against the execution
        contract; returns a list of human-readable problems (empty = ok).
        Accepts parsed=None (unparseable output) and reports it.

        v2 方案五: agent_id may be an executor id OR a business role id —
        role ids validate against valid_role_ids and resolve to their
        executor's input contract (node_name/command requirements).
        v2 P2+: node_required (name -> required params, derived from
        NodeRegistry params_schema) drives per-node required-param presence
        checks — replaces the old node_name-only check so a subtask that
        picks a real node but omits its mandatory params is rejected at
        planning time instead of failing at execution time."""
        if not isinstance(parsed, list) or not parsed:
            return ["output is not a non-empty JSON array"]
        known_agents = {"executor_node", "executor_workflow", "executor_mlx", "executor_shell"}
        # 方案五: role id -> executor mapping for contract validation
        role_map: Dict[str, str] = {}
        if valid_role_ids:
            try:
                from .role_registry import get_role_registry

                reg = get_role_registry()
                role_map = {rid: reg.executor_for(rid) for rid in valid_role_ids}
            except Exception:
                role_map = {}
        problems: list = []
        for i, st in enumerate(parsed):
            if not isinstance(st, dict):
                problems.append(f"subtask {i}: not an object")
                continue
            agent = str(st.get("agent_id", "executor_node"))
            if agent in role_map:
                executor = role_map[agent]  # business role id
            elif agent in known_agents:
                executor = agent
            else:
                problems.append(f"subtask {i}: unknown agent_id '{agent}'")
                continue
            inp = st.get("input_data")
            if not isinstance(inp, dict):
                problems.append(f"subtask {i}: input_data must be an OBJECT, got {type(inp).__name__}")
                continue
            if executor == "executor_node":
                node = str(inp.get("node_name") or "")
                if not node:
                    problems.append(f"subtask {i}: executor_node input_data missing node_name")
                elif valid_nodes and node not in valid_nodes:
                    problems.append(f"subtask {i}: node_name '{node}' not in catalog")
                else:
                    # v2 P2+: registry-derived required-param presence check —
                    # every mandatory param of the picked node must be present
                    for req in (node_required or {}).get(node, []):
                        if req not in inp:
                            problems.append(f"subtask {i}: node '{node}' missing required param '{req}'")
            elif executor == "executor_shell" and not inp.get("command"):
                problems.append(f"subtask {i}: executor_shell input_data missing command")
            elif executor == "executor_mlx" and not inp.get("prompt"):
                # 27B live-drill evidence: planner emitted an analyzer subtask
                # without "prompt" -> the executor failed with 缺少 prompt 参数
                problems.append(f"subtask {i}: executor_mlx input_data missing prompt")
            for d in st.get("depends_on") or []:
                try:
                    idx = int(d)
                except (TypeError, ValueError):
                    problems.append(f"subtask {i}: depends_on {d!r} is not an integer index")
                    continue
                # v2 方案一 (P1-5): negative indexes passed the old ">= i" check
                # but were then silently dropped at materialization — validate
                # the full legal range here so nothing slips through.
                if idx < 0:
                    problems.append(f"subtask {i}: depends_on {d} is negative")
                elif idx >= i:
                    problems.append(f"subtask {i}: depends_on {d} must reference an EARLIER index")
        return problems

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
        """获取任务状态（公共 API，避免外部访问 _tasks）。

        v2 方案三: also searches plan objects — plan-path subtasks never
        enter _tasks/_task_archive, so the acceptance gate previously
        returned "任务不存在" for every orchestrated plan subtask (same
        blind spot the dashboard had before the 24a89bc merge fix).
        """
        t = self._tasks.get(task_id) or self._task_archive.get(task_id)
        if t is not None:
            return t
        for p in self._plans.values():
            for pt in p.tasks:
                if pt.task_id == task_id:
                    return pt
        return None

    def _archive_task(self, task) -> None:
        """Bounded LRU archive of terminal tasks (acceptance gate needs
        finished tasks addressable; capped to keep memory flat)."""
        self._task_archive[task.task_id] = task
        self._task_archive.move_to_end(task.task_id)
        while len(self._task_archive) > 256:
            self._task_archive.popitem(last=False)

    def _write_plan_retrospective(self, plan, results: Dict[str, Any], elapsed: float) -> None:
        """Retrospective (audit 方案二③): append a plan terminal snapshot to
        the trajectory jsonl — task tree with parent links, per-task status/
        error/acceptance, and the honest plan verdict, so every run is
        reviewable afterwards. Best-effort: never breaks plan execution."""
        try:
            from .trajectory_writer import write_plan_retrospective

            write_plan_retrospective(plan, results, elapsed)
        except Exception as e:
            logger.debug(f"plan retrospective skipped: {e}")
