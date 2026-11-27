"""Workflow node base class — abstract base for all automation nodes.

Each node contains:
- input/output data definitions
- execution logic (execute method)
- config parameter validation
- execution status reporting

Consolidated from the Squish ToolRegistry pattern:
- param type coercion (_coerce_*), handling string-typed params from LLM output
- JSON Schema parameter validation
- tool name mapping
"""

from __future__ import annotations

import logging
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

# issue #494 (K2): node substrate delegates to fusion-harness dag_node —
# BaseNode subclasses the kernel BaseNode (id generation / node_type stamping
# shared) and NodeRegistry routes registration through a kernel NodeRegistry
# instance (dup-reject enforcement = kernel code, `existing is not node_class`).
# Cowork extensions kept on top, kernel semantics untouched:
#   - aliases / freeze_builtins / protected+builtin names / R-3 lock — cowork-only
#     hardening the kernel registry does not model
#   - coerce_params stays LOCAL lenient per-field (kernel coerce_params is strict
#     all-or-nothing: non-integral float "3.7" errors vs cowork truncates to 3;
#     comma-string array errors vs cowork splits; dict-level raise vs per-field
#     keep). LLM-generated params rely on lenient coercion — switching would
#     break the regression net. Parity gap filed upstream (fusion-harness issue).
try:
    from fusion_harness.dag_node import BaseNode as KernelBaseNode
    from fusion_harness.dag_node import NodeRegistry as KernelNodeRegistry
    from fusion_harness.dag_node import NodeRegistryError as KernelNodeRegistryError
except ImportError as exc:
    from ._harness_guard import harness_import_error

    raise harness_import_error(exc) from exc

logger = logging.getLogger(__name__)

# ── Param type coercion (absorbed from Squish tool_registry.py) ──
# Small/quantized models often output string scalars ("10", "true"),
# these functions coerce strings into the declared JSON Schema types,
# ensuring LLM-generated params parse and execute correctly.

_TRUE_STR = frozenset({"true", "1", "yes", "y", "on"})
_FALSE_STR = frozenset({"false", "0", "no", "n", "off"})


def _coerce_int(value: Any) -> Any:
    """Coerce a value to int."""
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return int(value)  # truncate floats to int
    if isinstance(value, str):
        s = value.strip()
        try:
            return int(s)
        except ValueError:
            pass
        try:
            f = float(s)
            return int(f)  # truncate float strings to int
        except ValueError:
            return value
    return value


def _coerce_number(value: Any) -> Any:
    """Coerce a value to float."""
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return value
    return value


def _coerce_bool(value: Any) -> Any:
    """Coerce a value to bool."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        s = value.strip().lower()
        if s in _TRUE_STR:
            return True
        if s in _FALSE_STR:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return value


def _coerce_array(value: Any) -> Any:
    """Coerce a value to list."""
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        s = value.strip()
        if s.startswith("[") and s.endswith("]"):
            try:
                import json

                return json.loads(s)
            except (json.JSONDecodeError, ValueError):
                pass
        # comma-separated string to list
        return [item.strip() for item in s.split(",") if item.strip()]
    if isinstance(value, (set, tuple, frozenset)):
        return list(value)
    return [value]


def coerce_param(value: Any, param_type: str) -> Any:
    """Coerce a parameter value according to the JSON Schema type.

    Args:
        value: raw value (possibly a string from LLM output)
        param_type: JSON Schema type ("integer", "number", "boolean", "array", "object", "string")

    Returns:
        The coerced value

    Consolidated from the _coerce_* family in Squish tool_registry.py.
    """
    if param_type == "integer":
        return _coerce_int(value)
    elif param_type == "number":
        return _coerce_number(value)
    elif param_type == "boolean":
        return _coerce_bool(value)
    elif param_type == "array":
        return _coerce_array(value)
    elif param_type == "object":
        if isinstance(value, str):
            try:
                import json

                return json.loads(value)
            except (json.JSONDecodeError, ValueError):
                return value
        if isinstance(value, dict):
            return value
        return {"value": value}
    return value


def coerce_params(
    params: Dict[str, Any],
    schema: Dict[str, Any],
) -> Dict[str, Any]:
    """Coerce a params dict in bulk according to the JSON Schema.

    Args:
        params: raw params dict (possibly from LLM output)
        schema: JSON Schema definition (from get_params_schema())

    Returns:
        The coerced params dict
    """
    properties = schema.get("properties", {})
    coerced = {}
    for key, value in params.items():
        if key in properties:
            param_type = properties[key].get("type", "string")
            coerced[key] = coerce_param(value, param_type)
        else:
            coerced[key] = value
    return coerced


class NodeStatus(Enum):
    """Node execution status."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    DENIED = "denied"
    # issue #260: waiting at HITL gate — visible on the timeline immediately, terminal node step appended later
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class NodeCategory(Enum):
    """Node category."""

    MACOS_SYSTEM = "macos_system"  # macOS system operations
    FILE_OPERATION = "file_operation"  # file operations
    AI_PROCESSING = "ai_processing"  # AI processing
    TOOL = "tool"  # general tools (added, from the Squish tool pattern)
    IO = "io"  # input/output
    LOGIC = "logic"  # logic control
    FUSION_ECOSYSTEM = "fusion_ecosystem"  # Fusion ecosystem interop
    # Same-value alias: business process source references the compliant name (TASK-CONFIG-2026-10-28,
    # FUSION_-prefixed names are out-of-contract variables in business repo source)
    ECOSYSTEM = "fusion_ecosystem"  # Fusion ecosystem interop (alias)


@dataclass
class NodeConfig:
    """Node configuration parameters."""

    label: str = ""
    description: str = ""
    continue_on_error: bool = False
    max_retries: int = 0
    retry_delay: float = 1.0
    timeout: float = 0.0  # 0 = no timeout
    # issue #182: business failures (NodeResult FAILED, not exceptions) are not retried by default; opt-in to join engine retries
    retry_on_business_failure: bool = False
    # issue #189: node executor (simple = direct node.execute, agent = delegate to fusion-agent-studio)
    executor: str = "simple"
    agent: str = ""
    tools: List[str] = field(default_factory=list)
    model: str = ""
    max_turns: int = 0
    budget_tokens: int = 0
    params: Dict[str, Any] = field(default_factory=dict)
    # issue #422: data-driven conditional skip — the node is marked SKIPPED when all conditions match
    # (verdict branch routing: extend skips new-only steps). Conditions look like
    # {"source": "<node_id>", "path": "a.b", "equals": x}; also supported:
    # not_equals / in / not_in; with source omitted, evaluation runs against the merged upstream input
    skip_if: List[Dict[str, Any]] = field(default_factory=list)
    # exp/dynamic-orchestration: LLM-informed one-shot repair after retry exhaustion.
    # Default OFF, zero behavior change. See docs/pr-repair-step.md.
    on_error_repair: bool = False
    # exp/degrade-semantics: when skip_if is fully hit, run the node anyway on
    # partial inputs (missing upstream outputs stay absent) and tag the step
    # output with "_degraded": <skip_desc>. Graceful degradation for terminal
    # nodes. Default OFF preserves issue #422 skip semantics exactly.
    degrade_on_skip: bool = False


@dataclass
class NodeResult:
    """Node execution result."""

    status: NodeStatus
    data: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    execution_time: float = 0.0
    output_files: List[str] = field(default_factory=list)
    summary: str = ""
    schema: Optional[Dict[str, Any]] = None

    def validate(self) -> bool:
        if self.schema and self.data is not None:
            from .schema import OutputSchema

            return OutputSchema.validate(self.data, self.schema)
        return True


class BaseNode(KernelBaseNode, ABC):
    """Base class for all automation nodes.

    A node is the basic unit of the whole workflow. Each node performs one specific operation,
    receives input data, produces output data, and passes it to the next node.

    issue #494: subclasses the fusion-harness kernel BaseNode — id
    generation (`node_<hex8>`) and the kernel `node_type` attribute come
    from the kernel; cowork adds config/status/`name`-keyed metadata.
    """

    # Node metadata (overridden by subclasses)
    name: str = "base"
    display_name: str = "基础节点"  # i18n: user-facing
    category: NodeCategory = NodeCategory.IO
    description: str = "基础节点"  # i18n: user-facing
    icon: str = "⚙️"
    default_label: str = "节点"  # i18n: user-facing

    # Input/output definitions (kernel BaseNode declares inputs/outputs as Dict — cowork
    # schema is a list of {key,label,type}; subclass attr wins)
    inputs: List[Dict[str, Any]] = []
    outputs: List[Dict[str, Any]] = [{"key": "output", "label": "输出", "type": "any"}]  # i18n: user-facing

    def __init__(self, node_id: str = "", config: Optional[NodeConfig] = None):
        super().__init__(node_id=node_id)
        self.config = config or NodeConfig()
        self.status = NodeStatus.PENDING
        self.result: Optional[NodeResult] = None

    @abstractmethod
    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        """Execute the node logic.

        Args:
            inputs: input data dict passed in from upstream nodes

        Returns:
            NodeResult: the node execution result
        """
        ...

    def validate_config(self) -> List[str]:
        """Validate node config, returning a list of errors. An empty list means the config is valid."""
        return []

    def watchdog_floor(self) -> float:
        # issue #378: node-resolved watchdog floor — param-driven deadlines (e.g.
        # claude_dispatch dispatch timeout) raise the engine watchdog floor so the
        # engine never kills a node before its own deadline; 0 = no override
        return 0.0

    def collect_outbox_effects(self) -> List[Dict[str, Any]]:
        # issue #382: outbox effects declared by this node, enqueued with the node
        # result in ONE transaction (engine/outbox.py record_step). Entries:
        # {node_id, action, payload, receipt_key?}. Default: no effects — nodes
        # performing side effects inline (receipts via outbox_ctx) declare none.
        return []

    def get_params_schema(self) -> Dict[str, Any]:
        """Return the JSON Schema of node params, used by the UI to auto-generate config forms."""
        return {
            "type": "object",
            "properties": {},
            "required": [],
        }

    @classmethod
    def _cached_default_schema(cls) -> Dict[str, Any]:
        # E-15: legacy list()/create() built a fresh instance per node to get the schema (47x construction + uuid side effects).
        # The schema depends only on the class (get_params_schema never reads self.config.params), so cache a class-level empty-config schema.
        cached = cls.__dict__.get("_schema_cache")
        if cached is not None:
            return cached
        schema = cls(NodeConfig()).get_params_schema()
        cls._schema_cache = schema
        return schema

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "display_name": self.display_name,
            "category": self.category.value,
            "description": self.description,
            "icon": self.icon,
            "config": {
                "label": self.config.label,
                "description": self.config.description,
                "continue_on_error": self.config.continue_on_error,
                "max_retries": self.config.max_retries,
                "retry_delay": self.config.retry_delay,
                "retry_on_business_failure": self.config.retry_on_business_failure,
                "timeout": self.config.timeout,
                "executor": self.config.executor,
                "agent": self.config.agent,
                "tools": self.config.tools,
                "model": self.config.model,
                "max_turns": self.config.max_turns,
                "budget_tokens": self.config.budget_tokens,
                "params": self.config.params,
                "skip_if": self.config.skip_if,
                "on_error_repair": self.config.on_error_repair,
                "degrade_on_skip": self.config.degrade_on_skip,
            },
            "status": self.status.value,
        }

    @classmethod
    def from_dict(cls, data: dict) -> BaseNode:
        """Deserialize a node from a dict."""
        cfg = data.get("config") or {}
        # issue #266: template/API-format nodes carry params at the top level — previously only config.params was read,
        # so params were lost after deserialization and the node re-ran with empty params after HITL approval recovery ("no command specified")
        if "params" in cfg:
            params = cfg["params"]
        else:
            params = data.get("params", {})
        config = NodeConfig(
            label=data.get("config", {}).get("label", ""),
            description=data.get("config", {}).get("description", ""),
            continue_on_error=data.get("config", {}).get("continue_on_error", False),
            max_retries=data.get("config", {}).get("max_retries", 0),
            retry_delay=data.get("config", {}).get("retry_delay", 1.0),
            retry_on_business_failure=data.get("config", {}).get("retry_on_business_failure", False),
            timeout=data.get("config", {}).get("timeout", 0.0),
            executor=data.get("config", {}).get("executor", "simple"),
            agent=data.get("config", {}).get("agent", ""),
            tools=list(data.get("config", {}).get("tools", []) or []),
            model=data.get("config", {}).get("model", ""),
            max_turns=int(data.get("config", {}).get("max_turns", 0) or 0),
            budget_tokens=int(data.get("config", {}).get("budget_tokens", 0) or 0),
            params=params or {},
            skip_if=[c for c in (data.get("config", {}).get("skip_if") or []) if isinstance(c, dict)],
            on_error_repair=bool(data.get("config", {}).get("on_error_repair", False)),
            degrade_on_skip=bool(data.get("config", {}).get("degrade_on_skip", False)),
        )
        return cls(node_id=data.get("id", ""), config=config)


class NodeRegistry:
    """Node type registry — manages all available node types.

    An n8n-like node type registration mechanism, used for:
    - node lookup and instantiation
    - node creation during template deserialization
    - UI node panel display

    Consolidated from Squish ToolRegistry:
    - param coercion (coerce_params)
    - param validation (validate_params)
    """

    _registry: Dict[str, type[BaseNode]] = {}
    # issue #494 (K2): kernel NodeRegistry instance — registration dup-reject
    # (`existing is not node_class`) is enforced by kernel code. Cowork keeps a
    # mirror dict for alias lookup / unregister / clear (kernel registry has no
    # removal API; rebuilds happen on those paths).
    _kernel_registry: KernelNodeRegistry = KernelNodeRegistry()
    # Tool name mapping table (absorbed from Squish tool_name_map.py)
    # maps user-friendly names ↔ backend node names
    _name_aliases: Dict[str, str] = {}
    # CR-20: registered-name protection — a plugin overriding a registered node → rejected (anti-hijack)
    # first registration is the baseline; re-registering the same class is idempotently allowed (import_all_nodes safe to call repeatedly)
    _protected_names: set[str] = set()
    # E-13: baseline builtin node names — freeze_builtins() freezes them after import_all_nodes.
    # unregister refuses builtin names (a malicious plugin node_map claiming shell_exec → unload deleting builtins).
    _builtin_names: set[str] = set()
    _builtins_frozen: bool = False
    # R-3: concurrency protection for class-level mutable state — check-then-set TOCTOU between register/clear and create/list.
    # RLock tolerates create()→get() re-entry; both decorator import-time and runtime concurrent registration are safe.
    _lock: threading.RLock = threading.RLock()

    @classmethod
    def register(cls, node_class: type[BaseNode], *, force: bool = False) -> type[BaseNode]:
        """Register a node type.

        CR-20: if name is already registered and the new class is not the registered class itself → reject the override (anti plugin hijack of builtin nodes).
        force=True explicitly overrides (internal unload-then-re-register scenarios).
        R-3: check-then-set under the lock, preventing concurrent-registration override races.
        """
        name = node_class.name
        with cls._lock:
            existing = cls._registry.get(name)
            if existing is not None and existing is not node_class and not force:
                logger.error(
                    f"节点类型 '{name}' 已注册为 {existing.__name__}, 拒绝 {node_class.__name__} 覆盖 (防劫持)"  # i18n: user-facing
                )
                return node_class
            # issue #494: kernel registry is the registration gate. force=True
            # replaces an existing class — kernel has no removal API, so the
            # kernel instance is rebuilt from the mirror (minus `name`) first.
            if force and existing is not None:
                cls._rebuild_kernel_registry(skip=name)
            try:
                cls._kernel_registry.register(node_class, node_type=name)
            except KernelNodeRegistryError:
                # kernel instance stale vs mirror (external _registry mutation,
                # e.g. test fixtures restoring the dict directly) — rebuild from
                # the mirror and retry once before giving up
                cls._rebuild_kernel_registry()
                try:
                    cls._kernel_registry.register(node_class, node_type=name)
                except KernelNodeRegistryError as exc:
                    logger.error(f"kernel node registration rejected '{name}': {exc}")
                    return node_class
            cls._registry[name] = node_class
            if name not in cls._protected_names:
                cls._protected_names.add(name)
        return node_class

    @classmethod
    def _rebuild_kernel_registry(cls, *, skip: str = "") -> None:
        # kernel NodeRegistry has no unregister/clear — rebuild from the mirror
        cls._kernel_registry = KernelNodeRegistry()
        for reg_name, reg_class in cls._registry.items():
            if reg_name == skip:
                continue
            try:
                cls._kernel_registry.register(reg_class, node_type=reg_name)
            except KernelNodeRegistryError as exc:
                logger.warning(f"kernel registry rebuild skipped '{reg_name}': {exc}")

    @classmethod
    def get(cls, name: str) -> Optional[type[BaseNode]]:
        """Get a node type (alias lookup supported)."""
        with cls._lock:
            # direct lookup first
            node_class = cls._registry.get(name)
            if node_class:
                return node_class
            # then via alias
            backend_name = cls._name_aliases.get(name)
            if backend_name:
                return cls._registry.get(backend_name)
            return None

    @classmethod
    def create(cls, name: str, **kwargs) -> Optional[BaseNode]:
        """Create a node instance (param coercion supported).

        When params are passed and the node has a schema, types are coerced automatically.
        Consolidated from the _coerce_* mechanism in Squish ToolRegistry.
        """
        node_class = cls.get(name)
        if node_class is None:
            logger.error(f"Unknown node type: {name}")
            return None

        # if params were passed, coerce types automatically
        if "config" in kwargs and kwargs["config"] is not None:
            config = kwargs["config"]
            if hasattr(config, "params") and config.params:
                # fetch the node schema for coercion (E-15: use the class-level cache to avoid per-request instance construction)
                try:
                    schema = node_class._cached_default_schema()
                    config.params = coerce_params(config.params, schema)
                except Exception as e:
                    logger.debug(f"Param coercion failed: {e}")

        return node_class(**kwargs)

    @classmethod
    def register_alias(cls, alias: str, target_name: str) -> None:
        """Register a tool name alias.

        Consolidated from the VSCODE_TO_BACKEND mapping pattern in Squish tool_name_map.py.
        Maps user-friendly names (e.g. "桌面清理") to backend node names ("desktop_clean").  # i18n: user-facing

        Args:
            alias: the alias (user-friendly name)
            target_name: the target node name
        """
        with cls._lock:
            if target_name not in cls._registry:
                logger.debug(
                    f"Alias '{alias}' → '{target_name}' target node not registered (resolves automatically after node import)"
                )
            cls._name_aliases[alias] = target_name
        logger.debug(f"Alias registered: {alias} → {target_name}")

    @classmethod
    def resolve_alias(cls, name: str) -> str:
        """Resolve an alias, returning the backend node name.

        Args:
            name: the user-entered name (possibly an alias)

        Returns:
            str: the backend node name
        """
        with cls._lock:
            if name in cls._registry:
                return name
            backend = cls._name_aliases.get(name)
            return backend or name

    @classmethod
    def list_aliases(cls) -> Dict[str, str]:
        """List all alias mappings."""
        with cls._lock:
            return dict(cls._name_aliases)

    @classmethod
    def list(cls, category: Optional[NodeCategory] = None) -> List[Dict[str, Any]]:
        """List all registered node types (optionally filtered by category)."""
        with cls._lock:
            items = list(cls._registry.items())
        result = []
        for name, node_class in items:
            if category and node_class.category != category:
                continue
            result.append(
                {
                    "name": name,
                    "display_name": node_class.display_name,
                    "category": node_class.category.value,
                    "description": node_class.description,
                    "icon": node_class.icon,
                    "default_label": node_class.default_label,
                    "params_schema": node_class._cached_default_schema(),
                }
            )
        return result

    @classmethod
    def freeze_builtins(cls, *, extra_names: Optional[set] = None) -> None:
        """E-13: freeze baseline builtin node names. Called after import_all_nodes.

        Only names registered by actual node modules are frozen (extra_names), not test fixtures or other non-module names.
        After freezing, unregister refuses builtin names (a malicious plugin node_map falsely claiming shell_exec →
        loader.unload calling unregister("shell_exec") deleting builtin nodes).

        Args:
            extra_names: node names newly registered by this module, accumulated into the existing builtin snapshot.
        """
        with cls._lock:
            if extra_names:
                cls._builtin_names |= extra_names
            cls._builtins_frozen = True
        logger.info(f"Baseline builtin nodes frozen: {len(cls._builtin_names)}")

    @classmethod
    def unregister(cls, name: str, *, force: bool = False) -> None:
        """Unregister a node type (also releases protection marks).

        E-13: builtin baseline nodes refuse unload (force=True explicitly overrides, internal unload-then-re-register scenarios only).
        """
        with cls._lock:
            if cls._builtins_frozen and name in cls._builtin_names and not force:
                logger.error(
                    f"Refusing to unload builtin node '{name}' (E-13: prevents plugin node_map hijack from deleting builtins)"
                )
                return
            cls._registry.pop(name, None)
            cls._protected_names.discard(name)
            cls._rebuild_kernel_registry()

    @classmethod
    def clear(cls) -> None:
        """Clear the registry."""
        with cls._lock:
            cls._registry.clear()
            cls._kernel_registry = KernelNodeRegistry()
            cls._protected_names.clear()
            cls._builtin_names.clear()
            cls._builtins_frozen = False


def register_node(func=None, *, name: str = ""):
    """Decorator: convenient node registration.

    Usage:
        @register_node
        class MyNode(BaseNode): ...

        @register_node(name="custom_name")
        class MyNode(BaseNode): ...
    """

    def wrapper(node_class):
        if name:
            node_class.name = name
        NodeRegistry.register(node_class)
        return node_class

    if func is not None:
        return wrapper(func)
    return wrapper
