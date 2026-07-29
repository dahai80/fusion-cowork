"""多智能体联动引擎 — 多个 Agent 协同完成复杂工作流。

V0.3 特性：
- Agent 注册与发现
- 任务分解与分配
- Agent 间通信与协调
- 结果聚合
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class AgentRole(Enum):
    """Agent 角色。"""
    PLANNER = "planner"          # 任务规划
    EXECUTOR = "executor"        # 任务执行
    ANALYZER = "analyzer"        # 结果分析
    VALIDATOR = "validator"      # 结果验证
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
    status: str = "pending"  # pending | running | completed | failed
    created_at: float = 0.0
    started_at: float = 0.0
    completed_at: float = 0.0
    error: str = ""


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

    def __init__(self):
        self._agents: Dict[str, Agent] = {}
        self._plans: Dict[str, OrchestrationPlan] = {}
        self._executors: Dict[str, Callable] = {}
        self._tasks: Dict[str, AgentTask] = {}
        self._runtimes: Dict[str, Any] = {}
        self._message_bus = None

    def register_default_agents(self) -> None:
        """注册默认 Agent + 执行器。"""
        from .executors import DEFAULT_EXECUTORS, CoordinatorExecutor
        from .comm import AgentMessageBus

        if not self._message_bus:
            self._message_bus = AgentMessageBus()

        default_agents = [
            Agent(agent_id="planner", name="规划者", role=AgentRole.PLANNER,
                  description="任务规划与分解", capabilities=["plan", "decompose"]),
            Agent(agent_id="coordinator", name="协调者", role=AgentRole.COORDINATOR,
                  description="协调子任务分发", capabilities=["coordinate", "dispatch"]),
            Agent(agent_id="executor_node", name="节点执行者", role=AgentRole.EXECUTOR,
                  description="执行 NodeRegistry 节点", capabilities=["node_exec"]),
            Agent(agent_id="executor_workflow", name="工作流执行者", role=AgentRole.EXECUTOR,
                  description="执行工作流模板", capabilities=["workflow_exec"]),
            Agent(agent_id="executor_mlx", name="AI 执行者", role=AgentRole.EXECUTOR,
                  description="调用 MLX AI 服务", capabilities=["mlx_chat", "mlx_classify", "mlx_summarize"]),
            Agent(agent_id="executor_shell", name="命令执行者", role=AgentRole.EXECUTOR,
                  description="执行 Shell 命令", capabilities=["shell_exec"]),
            Agent(agent_id="analyzer", name="分析者", role=AgentRole.ANALYZER,
                  description="结果分析与总结", capabilities=["analyze", "summarize"]),
            Agent(agent_id="validator", name="验证者", role=AgentRole.VALIDATOR,
                  description="结果验证与质量检查", capabilities=["validate", "check"]),
        ]

        for agent in default_agents:
            self.register_agent(agent)

        for agent_id, executor in DEFAULT_EXECUTORS.items():
            self.register_executor(agent_id, executor)

        self.register_executor("planner", DEFAULT_EXECUTORS["executor_mlx"])
        self.register_executor("analyzer", DEFAULT_EXECUTORS["executor_mlx"])
        self.register_executor("validator", DEFAULT_EXECUTORS["executor_mlx"])

        self._coordinator_executor = CoordinatorExecutor(self)
        self.register_executor("coordinator", self._coordinator_executor)

        logger.info(f"默认 Agent 注册完成: {len(self._agents)} 个 Agent, {len(self._executors)} 个执行器")

    async def submit_task(self, description: str, input_data: Dict[str, Any] = None) -> str:
        """提交简单任务 — 自动选择执行器。

        Returns:
            task_id
        """
        task_id = f"task_{uuid.uuid4().hex[:8]}"
        task = AgentTask(
            task_id=task_id,
            agent_id="executor_node",
            description=description,
            input_data=input_data or {"prompt": description},
            created_at=time.time(),
        )
        self._tasks[task_id] = task

        # 后台执行
        asyncio.create_task(self._run_submitted_task(task))

        return task_id

    async def _run_submitted_task(self, task: AgentTask) -> None:
        """执行提交的任务。"""
        task.status = "running"
        task.started_at = time.time()

        executor = self._executors.get(task.agent_id)
        if executor:
            try:
                if asyncio.iscoroutinefunction(executor):
                    result = await executor(task.input_data)
                else:
                    result = executor(task.input_data)
                task.output_data = result if isinstance(result, dict) else {"result": result}
                task.status = "completed"
            except Exception as e:
                task.error = str(e)
                task.status = "failed"
                logger.error(f"提交任务执行异常: {e}")
        else:
            task.output_data = {"status": "simulated", "input": task.input_data}
            task.status = "completed"

        task.completed_at = time.time()

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
        """执行编排计划。"""
        plan = self._plans.get(plan_id)
        if not plan:
            return {"error": f"计划不存在: {plan_id}"}

        plan.status = "running"
        results = {}
        start_time = time.time()

        # 拓扑排序执行
        executed = set()
        while len(executed) < len(plan.tasks):
            # 找出可执行的任务
            ready = []
            for task in plan.tasks:
                if task.task_id in executed:
                    continue
                deps = plan.dependencies.get(task.task_id, [])
                if all(d in executed for d in deps):
                    ready.append(task)

            if not ready:
                # 死锁检测
                remaining = [t.task_id for t in plan.tasks if t.task_id not in executed]
                logger.error(f"任务死锁: {remaining}")
                plan.status = "failed"
                return {"error": f"任务死锁: {remaining}"}

            # 并行执行就绪任务
            tasks = [self._execute_task(task, plan) for task in ready]
            task_results = await asyncio.gather(*tasks, return_exceptions=True)

            for task, result in zip(ready, task_results):
                executed.add(task.task_id)
                if isinstance(result, Exception):
                    results[task.task_id] = {"error": str(result)}
                    task.status = "failed"
                    task.error = str(result)
                else:
                    results[task.task_id] = result
                    task.status = "completed"
                    task.output_data = result or {}
                    task.completed_at = time.time()

        plan.status = "completed"
        elapsed = time.time() - start_time
        logger.info(f"编排完成: {plan_id} ({elapsed:.2f}s)")

        return {
            "plan_id": plan_id,
            "status": "completed",
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
                if asyncio.iscoroutinefunction(executor):
                    result = await executor(task.input_data)
                else:
                    result = executor(task.input_data)
                return result if isinstance(result, dict) else {"result": result}
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
                    result = await fallback(task.input_data)
                    return result if isinstance(result, dict) else {"result": result}
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
        """运行标准编排流水线。

        流程：Planner → Executor → Analyzer → Validator
        """
        # 1. Planner 规划
        planner = self.get_agents_by_role(AgentRole.PLANNER)
        if not planner:
            return {"error": "无 Planner Agent"}

        plan = await self.create_plan("standard_pipeline", "标准编排流水线")
        plan_task = self.add_task(plan.plan_id, planner[0].agent_id, "任务规划", input_data)

        # 2. Executor 执行
        executors = self.get_agents_by_role(AgentRole.EXECUTOR)
        if executors:
            for i, executor in enumerate(executors[:3]):
                self.add_task(
                    plan.plan_id, executor.agent_id,
                    f"执行任务 {i+1}", input_data,
                    depends_on=[plan_task.task_id] if plan_task else [],
                )

        # 3. Analyzer 分析
        analyzers = self.get_agents_by_role(AgentRole.ANALYZER)
        if analyzers:
            executor_tasks = [t.task_id for t in plan.tasks if t.agent_id != (planner[0].agent_id if planner else "")]
            self.add_task(plan.plan_id, analyzers[0].agent_id, "结果分析", {}, depends_on=executor_tasks)

        # 4. 执行
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
        """停止所有 Runtime。"""
        for runtime in self._runtimes.values():
            await runtime.stop()
        self._runtimes.clear()
        logger.info("所有 AgentRuntime 已停止")

    def get_message_bus(self):
        """获取消息总线。"""
        return self._message_bus

    def get_task(self, task_id: str):
        """获取任务状态（公共 API，避免外部访问 _tasks）。"""
        return self._tasks.get(task_id)