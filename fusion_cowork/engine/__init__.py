"""Engine module exports.

Consolidated from the Squish architecture pattern:
- param type coercion: coerce_param, coerce_params
- tool name aliases: NodeRegistry.register_alias, resolve_alias

Added in V0.2:
- enhanced scheduler: EnhancedScheduler (calendar view, dependency orchestration, stats reports)
- workflow optimizer: WorkflowOptimizer (AI analysis, bottleneck detection, auto-fix)

Added in V0.3:
- permission management: PermissionManager, PermissionLevel, Permission
- hook system: HookManager, HookEvent, HookContext

Added in V0.4:
- AST diff transport: compute_ast_diff, apply_ast_diff (migrated from fusion-multi-node)

#521 drop-fallback ruling: importing this package asserts fusion-harness
availability — hooks/permission/compactor/funnels shims hard-import the
kernel and fail with an actionable install hint. Missing harness is a
startup error, never a degraded mode.
"""

from .ast_diff import apply_ast_diff, compute_ast_diff
from .compactor import CompactionConfig, Compactor, RetentionList
from .enhanced_scheduler import EnhancedScheduler, TaskDependency, TaskExecution
from .events import EventEmitter, EventType, WorkflowEvent
from .funnels import EventDomain, LifecycleBus, LifecycleEvent, LLMFunnel, ToolFunnel
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
from .run_persistence import RunStateStore
from .scheduler import ScheduledTask, TaskScheduler, TaskStatus
from .schema import OutputSchema
from .session import Session, SessionStore
from .workflow import Edge, Workflow, WorkflowEngine, WorkflowExecution, WorkflowStatus, WorkflowStep

__all__ = [
    # Nodes
    "BaseNode",
    "NodeConfig",
    "NodeResult",
    "NodeStatus",
    "NodeCategory",
    "NodeRegistry",
    "register_node",
    # Param coercion
    "coerce_param",
    "coerce_params",
    "_coerce_int",
    "_coerce_number",
    "_coerce_bool",
    "_coerce_array",
    # Workflow
    "Workflow",
    "WorkflowEngine",
    "WorkflowExecution",
    "WorkflowStatus",
    "Edge",
    "WorkflowStep",
    # Scheduling
    "TaskScheduler",
    "ScheduledTask",
    "TaskStatus",
    # V0.2 enhanced scheduler
    "EnhancedScheduler",
    "TaskExecution",
    "TaskDependency",
    # V0.2 AI optimization
    "WorkflowOptimizer",
    "OptimizationSuggestion",
    "WorkflowAnalysis",
    # V0.3 permissions
    "PermissionManager",
    "PermissionLevel",
    "Permission",
    # 111: fusion-harness kernel adapters
    "Compactor",
    "CompactionConfig",
    "RetentionList",
    "LifecycleBus",
    "LifecycleEvent",
    "EventDomain",
    "LLMFunnel",
    "ToolFunnel",
    # V0.3 hooks
    "HookManager",
    "HookEvent",
    "HookContext",
    # V0.3 sessions
    "Session",
    "SessionStore",
    # V0.3 events
    "EventType",
    "WorkflowEvent",
    "EventEmitter",
    # M4 structured output
    "OutputSchema",
    # V0.4 AST Diff
    "compute_ast_diff",
    "apply_ast_diff",
    # issue #191: run-control state persistence layer
    "RunStateStore",
]
