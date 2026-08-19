"""RPC 桥接层 — 托管 /rpc 端点, 委托 plugins/* 方法给 fusion-plugins-ecosystem。

Issue #48: fusion-studio 插件生态集成面板通过 POST /rpc 消费 15 个 plugins/* 方法。
本模块构造 fusion-plugins-ecosystem.MCPHandler, 注入 fusion-cowork runtime 句柄,
使 Studio 的 JSON-RPC 请求路由到上层 handler。

设计:
- 懒加载: 首次请求 /rpc 时构造 MCPHandler, 避免无 plugins 依赖环境的启动开销
- 降级: fusion-plugins-ecosystem 未安装时, /rpc 返回 -32603 + 可操作提示
- 节点发现: DeskRuntime 注入 NodeRegistry, desk.list_nodes() 返回 cowork 已注册节点
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# plugins/* 方法白名单 (Studio PluginBridge.swift 调用的 15 个)
PLUGINS_METHODS = frozenset(
    {
        "plugins.ping",
        "plugins/list",
        "plugins/install",
        "plugins/uninstall",
        "plugins/config.get",
        "plugins/config.set",
        "plugins/states",
        "plugins/state.get",
        "plugins/state.list",
        "plugins/token.records",
        "plugins/token.prune",
        "plugins/vram.usage",
        "plugins/logs.stream",
        "plugins/mcp.sessions",
        "plugins/mcp.sessions.prune",
    }
)

_HANDLER: Optional[Any] = None
_DESK_RUNTIME: Optional[Any] = None


def _build_desk_runtime() -> Any:
    """构造 DeskRuntime, 注入 fusion-cowork runtime 句柄。"""
    from fusion_plugins_ecosystem.desk_runtime import DeskRuntime

    from ..engine.node import NodeRegistry
    from ..engine.scheduler import TaskScheduler

    try:
        from ..ai.mlx_client import FusionMLXClient

        mlx_client = FusionMLXClient()
    except Exception as e:
        logger.warning("rpc_bridge: FusionMLXClient 构造失败, 降级为 None: %s", e)
        mlx_client = None

    desk = DeskRuntime(
        node_registry=NodeRegistry,
        task_scheduler=TaskScheduler(),
        mlx_client=mlx_client,
    )
    logger.info("rpc_bridge: DeskRuntime 已构造, 注入 NodeRegistry + TaskScheduler")
    return desk


def get_plugins_handler() -> Any:
    """懒加载 MCPHandler 单例 (注入 cowork runtime)。

    返回 fusion-plugins-ecosystem.MCPHandler 实例。
    依赖缺失时抛 ImportError, 由调用方转 JSON-RPC 错误。
    """
    global _HANDLER, _DESK_RUNTIME
    if _HANDLER is not None:
        return _HANDLER

    from fusion_plugins_ecosystem.jsonrpc import MCPHandler
    from fusion_plugins_ecosystem.registry import PluginRegistry

    _DESK_RUNTIME = _build_desk_runtime()
    registry = PluginRegistry(desk=_DESK_RUNTIME)
    _HANDLER = MCPHandler(registry=registry, desk=_DESK_RUNTIME)
    logger.info("rpc_bridge: MCPHandler 已构造 (plugins/* 委托就绪)")
    return _HANDLER


def is_plugins_available() -> bool:
    """检查 fusion-plugins-ecosystem 是否可导入 (不构造 handler)。"""
    try:
        import fusion_plugins_ecosystem  # noqa: F401

        return True
    except ImportError:
        return False


async def dispatch_rpc(request: Dict[str, Any]) -> Dict[str, Any]:
    """分发 /rpc JSON-RPC 请求到 MCPHandler。

    Args:
        request: 完整 JSON-RPC 2.0 请求 dict

    Returns:
        JSON-RPC 2.0 响应 dict (含 result 或 error)
    """
    request_id = request.get("id")
    method = request.get("method", "")

    if method not in PLUGINS_METHODS and not method.startswith("plugins."):
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {
                "code": -32601,
                "message": f"Method not found: {method} (/rpc 仅托管 plugins/*)",
            },
        }

    try:
        handler = get_plugins_handler()
    except ImportError as e:
        logger.error("rpc_bridge: fusion-plugins-ecosystem 不可用: %s", e)
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {
                "code": -32603,
                "message": (
                    "plugins runtime 未安装: pip install fusion-plugins-ecosystem"
                ),
            },
        }

    try:
        response = await handler.handle(request)
        if response is None:
            return {"jsonrpc": "2.0", "result": None, "id": request_id}
        return response
    except Exception as e:
        logger.error("rpc_bridge: dispatch %s 异常: %s", method, e)
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32603, "message": f"Internal error: {e}"},
        }


def list_nodes_for_plugins() -> list:
    """供 desk.list_nodes() 使用的节点发现桥 (issue #48 第 3 点)。

    返回 cowork NodeRegistry.list() 的结果; 依赖缺失时返回空列表。
    """
    if _DESK_RUNTIME is None or _DESK_RUNTIME.node_registry is None:
        try:
            from ..engine.node import NodeRegistry

            return NodeRegistry.list()
        except Exception as e:
            logger.warning("rpc_bridge: 节点发现失败: %s", e)
            return []
    return _DESK_RUNTIME.node_registry.list()
