"""Fusion-Desk 多智能体编排模块。"""

from .orchestrator import AgentOrchestrator, Agent, AgentRole, AgentTask, OrchestrationPlan
from .executors import NodeExecutor, WorkflowExecutor, MLXExecutor, ShellExecutor, CoordinatorExecutor, DEFAULT_EXECUTORS
from .comm import AgentMessageBus, AgentMessage
from .agent_runtime import AgentRuntime

__all__ = [
    "AgentOrchestrator", "Agent", "AgentRole", "AgentTask", "OrchestrationPlan",
    "NodeExecutor", "WorkflowExecutor", "MLXExecutor", "ShellExecutor", "CoordinatorExecutor", "DEFAULT_EXECUTORS",
    "AgentMessageBus", "AgentMessage",
    "AgentRuntime",
]