"""工作流节点基类 — 所有自动化节点的抽象基类。

每个节点包含：
- 输入/输出数据定义
- 执行逻辑（execute 方法）
- 配置参数校验
- 执行状态报告

整合自 Squish ToolRegistry 模式：
- 参数类型强制转换（_coerce_*），处理 LLM 输出的字符串类型参数
- JSON Schema 参数校验
- 工具名称映射
"""

from __future__ import annotations

import logging
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── 参数类型强制转换（吸纳自 Squish tool_registry.py） ──
# 小型/量化模型经常输出字符串标量（"10", "true"），
# 这些函数自动将字符串强制转换为 JSON Schema 声明的类型，
# 确保 LLM 生成的参数能被正确解析执行。

_TRUE_STR = frozenset({"true", "1", "yes", "y", "on"})
_FALSE_STR = frozenset({"false", "0", "no", "n", "off"})


def _coerce_int(value: Any) -> Any:
    """将值强制转换为 int。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return int(value)  # 截断浮点数为整型
    if isinstance(value, str):
        s = value.strip()
        try:
            return int(s)
        except ValueError:
            pass
        try:
            f = float(s)
            return int(f)  # 截断浮点数字符串为整型
        except ValueError:
            return value
    return value


def _coerce_number(value: Any) -> Any:
    """将值强制转换为 float。"""
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return value
    return value


def _coerce_bool(value: Any) -> Any:
    """将值强制转换为 bool。"""
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
    """将值强制转换为 list。"""
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
        # 逗号分隔的字符串转为列表
        return [item.strip() for item in s.split(",") if item.strip()]
    if isinstance(value, (set, tuple, frozenset)):
        return list(value)
    return [value]


def coerce_param(value: Any, param_type: str) -> Any:
    """根据 JSON Schema 类型强制转换参数值。

    Args:
        value: 原始值（可能来自 LLM 输出的字符串）
        param_type: JSON Schema 类型（"integer", "number", "boolean", "array", "object", "string"）

    Returns:
        强制转换后的值

    整合自 Squish tool_registry.py 的 _coerce_* 函数族。
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
    """根据 JSON Schema 批量强制转换参数字典。

    Args:
        params: 原始参数字典（可能来自 LLM 输出）
        schema: JSON Schema 定义（来自 get_params_schema()）

    Returns:
        强制转换后的参数字典
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
    """节点执行状态。"""

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    DENIED = "denied"
    CANCELLED = "cancelled"


class NodeCategory(Enum):
    """节点分类。"""

    MACOS_SYSTEM = "macos_system"  # macOS 系统操作
    FILE_OPERATION = "file_operation"  # 文件操作
    AI_PROCESSING = "ai_processing"  # AI 处理
    TOOL = "tool"  # 通用工具（新增，来自 Squish 工具模式）
    IO = "io"  # 输入/输出
    LOGIC = "logic"  # 逻辑控制
    FUSION_ECOSYSTEM = "fusion_ecosystem"  # Fusion 生态互通


@dataclass
class NodeConfig:
    """节点配置参数。"""

    label: str = ""
    description: str = ""
    continue_on_error: bool = False
    max_retries: int = 0
    retry_delay: float = 1.0
    timeout: float = 0.0  # 0 = no timeout
    params: Dict[str, Any] = field(default_factory=dict)


@dataclass
class NodeResult:
    """节点执行结果。"""

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


class BaseNode(ABC):
    """所有自动化节点的基类。

    节点是整个工作流的基本单元。每个节点执行一个特定操作，
    接收输入数据，产生输出数据，传递给下一个节点。
    """

    # 节点元数据（子类覆盖）
    name: str = "base"
    display_name: str = "基础节点"
    category: NodeCategory = NodeCategory.IO
    description: str = "基础节点"
    icon: str = "⚙️"
    default_label: str = "节点"

    # 输入/输出定义
    inputs: List[Dict[str, Any]] = []
    outputs: List[Dict[str, Any]] = [{"key": "output", "label": "输出", "type": "any"}]

    def __init__(self, node_id: str = "", config: Optional[NodeConfig] = None):
        self.id = node_id or f"node_{uuid.uuid4().hex[:8]}"
        self.config = config or NodeConfig()
        self.status = NodeStatus.PENDING
        self.result: Optional[NodeResult] = None

    @abstractmethod
    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        """执行节点逻辑。

        Args:
            inputs: 上游节点传入的输入数据字典

        Returns:
            NodeResult: 节点执行结果
        """
        ...

    def validate_config(self) -> List[str]:
        """校验节点配置，返回错误列表。空列表表示配置有效。"""
        return []

    def get_params_schema(self) -> Dict[str, Any]:
        """返回节点参数的 JSON Schema，用于 UI 自动生成配置表单。"""
        return {
            "type": "object",
            "properties": {},
            "required": [],
        }

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
                "timeout": self.config.timeout,
                "params": self.config.params,
            },
            "status": self.status.value,
        }

    @classmethod
    def from_dict(cls, data: dict) -> BaseNode:
        """从字典反序列化节点。"""
        config = NodeConfig(
            label=data.get("config", {}).get("label", ""),
            description=data.get("config", {}).get("description", ""),
            continue_on_error=data.get("config", {}).get("continue_on_error", False),
            max_retries=data.get("config", {}).get("max_retries", 0),
            retry_delay=data.get("config", {}).get("retry_delay", 1.0),
            timeout=data.get("config", {}).get("timeout", 0.0),
            params=data.get("config", {}).get("params", {}),
        )
        return cls(node_id=data.get("id", ""), config=config)


class NodeRegistry:
    """节点类型注册表 — 管理所有可用的节点类型。

    类似 n8n 的节点类型注册机制，用于：
    - 节点查找和实例化
    - 模板反序列化时的节点创建
    - UI 节点面板展示

    整合自 Squish ToolRegistry：
    - 参数强制转换（coerce_params）
    - 参数校验（validate_params）
    """

    _registry: Dict[str, type[BaseNode]] = {}
    # 工具名称映射表（吸纳自 Squish tool_name_map.py）
    # 用于用户友好的名称 ↔ 后端节点名称的转换
    _name_aliases: Dict[str, str] = {}
    # CR-20: 已注册名保护 — 插件覆盖已注册节点 → 拒绝 (防劫持)
    # 首次注册视为基线; 同类重注册幂等放行 (import_all_nodes 多次调用安全)
    _protected_names: set[str] = set()

    @classmethod
    def register(cls, node_class: type[BaseNode], *, force: bool = False) -> type[BaseNode]:
        """注册一个节点类型。

        CR-20: 若 name 已注册且新类非已注册类本身 → 拒绝覆盖 (防插件劫持内置节点)。
        force=True 显式覆盖 (内部卸载后重注册场景)。
        """
        name = node_class.name
        existing = cls._registry.get(name)
        if existing is not None and existing is not node_class and not force:
            logger.error(f"节点类型 '{name}' 已注册为 {existing.__name__}, 拒绝 {node_class.__name__} 覆盖 (防劫持)")
            return node_class
        cls._registry[name] = node_class
        if name not in cls._protected_names:
            cls._protected_names.add(name)
        return node_class

    @classmethod
    def get(cls, name: str) -> Optional[type[BaseNode]]:
        """获取节点类型（支持别名查找）。"""
        # 先直接查找
        node_class = cls._registry.get(name)
        if node_class:
            return node_class
        # 再通过别名查找
        backend_name = cls._name_aliases.get(name)
        if backend_name:
            return cls._registry.get(backend_name)
        return None

    @classmethod
    def create(cls, name: str, **kwargs) -> Optional[BaseNode]:
        """创建节点实例（支持参数强制转换）。

        如果传入 params 且节点有 schema，自动进行类型强制转换。
        整合自 Squish ToolRegistry 的 _coerce_* 机制。
        """
        node_class = cls.get(name)
        if node_class is None:
            logger.error(f"未知节点类型: {name}")
            return None

        # 检查是否传入 params 参数，如果有则自动进行类型强制转换
        if "config" in kwargs and kwargs["config"] is not None:
            config = kwargs["config"]
            if hasattr(config, "params") and config.params:
                # 获取节点 schema 进行参数强制转换
                try:
                    schema = node_class(NodeConfig()).get_params_schema()
                    config.params = coerce_params(config.params, schema)
                except Exception as e:
                    logger.debug(f"参数强制转换失败: {e}")

        return node_class(**kwargs)

    @classmethod
    def register_alias(cls, alias: str, target_name: str) -> None:
        """注册工具名称别名。

        整合自 Squish tool_name_map.py 的 VSCODE_TO_BACKEND 映射模式。
        用于用户友好的名称（如"桌面清理"）映射到后端节点名（"desktop_clean"）。

        Args:
            alias: 别名（用户友好名称）
            target_name: 目标节点名称
        """
        if target_name not in cls._registry:
            logger.debug(f"别名 '{alias}' → '{target_name}' 目标节点未注册 (节点导入后自动解析)")
        cls._name_aliases[alias] = target_name
        logger.debug(f"注册别名: {alias} → {target_name}")

    @classmethod
    def resolve_alias(cls, name: str) -> str:
        """解析别名，返回后端节点名称。

        Args:
            name: 用户输入的名称（可能是别名）

        Returns:
            str: 后端节点名称
        """
        if name in cls._registry:
            return name
        backend = cls._name_aliases.get(name)
        return backend or name

    @classmethod
    def list_aliases(cls) -> Dict[str, str]:
        """列出所有别名映射。"""
        return dict(cls._name_aliases)

    @classmethod
    def list(cls, category: Optional[NodeCategory] = None) -> List[Dict[str, Any]]:
        """列出所有注册的节点类型（可选按分类过滤）。"""
        result = []
        for name, node_class in cls._registry.items():
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
                    "params_schema": node_class(NodeConfig()).get_params_schema(),
                }
            )
        return result

    @classmethod
    def unregister(cls, name: str) -> None:
        """注销节点类型 (同时释放保护标记)。"""
        cls._registry.pop(name, None)
        cls._protected_names.discard(name)

    @classmethod
    def clear(cls) -> None:
        """清空注册表。"""
        cls._registry.clear()
        cls._protected_names.clear()


def register_node(func=None, *, name: str = ""):
    """装饰器：便捷注册节点。

    用法:
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
