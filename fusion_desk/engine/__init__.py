"""引擎模块导出。

整合自 Squish 架构模式：
- 参数类型强制转换：coerce_param, coerce_params
- 工具名称别名：NodeRegistry.register_alias, resolve_alias
"""

from .node import (
    BaseNode, NodeConfig, NodeResult, NodeStatus, NodeCategory,
    NodeRegistry, register_node,
    coerce_param, coerce_params, _coerce_int, _coerce_number, _coerce_bool, _coerce_array,
)
from .workflow import Workflow, WorkflowEngine, WorkflowExecution, WorkflowStatus, Edge, WorkflowStep
from .scheduler import TaskScheduler, ScheduledTask, TaskStatus

__all__ = [
    # 节点
    "BaseNode", "NodeConfig", "NodeResult", "NodeStatus", "NodeCategory",
    "NodeRegistry", "register_node",
    # 参数强制转换
    "coerce_param", "coerce_params", "_coerce_int", "_coerce_number", "_coerce_bool", "_coerce_array",
    # 工作流
    "Workflow", "WorkflowEngine", "WorkflowExecution", "WorkflowStatus", "Edge", "WorkflowStep",
    # 调度
    "TaskScheduler", "ScheduledTask", "TaskStatus",
]