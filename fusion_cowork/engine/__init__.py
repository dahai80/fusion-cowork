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

V0.4 新增：
- AST Diff 传输：compute_ast_diff, apply_ast_diff（迁移自 fusion-multi-node）
"""

from .ast_diff import apply_ast_diff, compute_ast_diff
from .enhanced_scheduler import EnhancedScheduler, TaskDependency, TaskExecution
from .events import EventEmitter, EventType, WorkflowEvent
from .hooks import HookContext, HookEvent, HookManager
from .node import (
    BaseNode,
    NodeCategory,
    NodeConfig,
    NodeRegistry,
    NodeResult,
    NodeStatus,
    _coerce_array,
    _coerce_bool,
    _coerce_int,
    _coerce_number,
    coerce_param,
    coerce_params,
    register_node,
)
from .optimizer import OptimizationSuggestion, WorkflowAnalysis, WorkflowOptimizer
from .permission import Permission, PermissionLevel, PermissionManager
from .scheduler import ScheduledTask, TaskScheduler, TaskStatus
from .schema import OutputSchema
from .session import Session, SessionStore
from .workflow import Edge, Workflow, WorkflowEngine, WorkflowExecution, WorkflowStatus, WorkflowStep

__all__ = [
    # 节点
    "BaseNode",
    "NodeConfig",
    "NodeResult",
    "NodeStatus",
    "NodeCategory",
    "NodeRegistry",
    "register_node",
    # 参数强制转换
    "coerce_param",
    "coerce_params",
    "_coerce_int",
    "_coerce_number",
    "_coerce_bool",
    "_coerce_array",
    # 工作流
    "Workflow",
    "WorkflowEngine",
    "WorkflowExecution",
    "WorkflowStatus",
    "Edge",
    "WorkflowStep",
    # 调度
    "TaskScheduler",
    "ScheduledTask",
    "TaskStatus",
    # V0.2 增强调度
    "EnhancedScheduler",
    "TaskExecution",
    "TaskDependency",
    # V0.2 AI 优化
    "WorkflowOptimizer",
    "OptimizationSuggestion",
    "WorkflowAnalysis",
    # V0.3 权限
    "PermissionManager",
    "PermissionLevel",
    "Permission",
    # V0.3 Hook
    "HookManager",
    "HookEvent",
    "HookContext",
    # V0.3 会话
    "Session",
    "SessionStore",
    # V0.3 事件
    "EventType",
    "WorkflowEvent",
    "EventEmitter",
    # M4 结构化输出
    "OutputSchema",
    # V0.4 AST Diff
    "compute_ast_diff",
    "apply_ast_diff",
]
