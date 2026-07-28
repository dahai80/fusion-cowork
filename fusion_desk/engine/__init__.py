"""引擎模块导出。

整合自 Squish 架构模式：
- 参数类型强制转换：coerce_param, coerce_params
- 工具名称别名：NodeRegistry.register_alias, resolve_alias

V0.2 新增：
- 增强调度器：EnhancedScheduler（日历视图、依赖编排、统计报表）
- 工作流优化器：WorkflowOptimizer（AI 分析、瓶颈检测、自动修复）

V0.3 新增：
- 权限管理：PermissionManager, PermissionLevel, Permission
- Hook系统：HookManager, HookEvent, HookContext
"""

from .node import (
    BaseNode, NodeConfig, NodeResult, NodeStatus, NodeCategory,
    NodeRegistry, register_node,
    coerce_param, coerce_params, _coerce_int, _coerce_number, _coerce_bool, _coerce_array,
)
from .workflow import Workflow, WorkflowEngine, WorkflowExecution, WorkflowStatus, Edge, WorkflowStep
from .scheduler import TaskScheduler, ScheduledTask, TaskStatus
from .enhanced_scheduler import EnhancedScheduler, TaskExecution, TaskDependency
from .optimizer import WorkflowOptimizer, OptimizationSuggestion, WorkflowAnalysis
from .permission import PermissionManager, PermissionLevel, Permission
from .hooks import HookManager, HookEvent, HookContext
from .session import Session, SessionStore
from .events import EventType, WorkflowEvent, EventEmitter

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
    # V0.2 增强调度
    "EnhancedScheduler", "TaskExecution", "TaskDependency",
    # V0.2 AI 优化
    "WorkflowOptimizer", "OptimizationSuggestion", "WorkflowAnalysis",
    # V0.3 权限
    "PermissionManager", "PermissionLevel", "Permission",
    # V0.3 Hook
    "HookManager", "HookEvent", "HookContext",
    # V0.3 会话
    "Session", "SessionStore",
    # V0.3 事件
    "EventType", "WorkflowEvent", "EventEmitter",
]