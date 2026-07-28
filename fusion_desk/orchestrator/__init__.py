"""Fusion-Desk 多智能体编排模块。"""

from .orchestrator import AgentOrchestrator, Agent, AgentRole, AgentTask, OrchestrationPlan
from .executors import NodeExecutor, WorkflowExecutor, MLXExecutor, ShellExecutor, DEFAULT_EXECUTORS
from .comm import AgentMessageBus, AgentMessage

__all__ = [
    "AgentOrchestrator", "Agent", "AgentRole", "AgentTask", "OrchestrationPlan",
    "NodeExecutor", "WorkflowExecutor", "MLXExecutor", "ShellExecutor", "DEFAULT_EXECUTORS",
    "AgentMessageBus", "AgentMessage",
]