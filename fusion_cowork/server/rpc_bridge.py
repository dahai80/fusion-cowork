"""RPC bridge layer — hosts the /rpc endpoint, delegating plugins/* methods to fusion-plugins-ecosystem.

Issue #48: the fusion-studio plugin ecosystem integration panel consumes 15 plugins/* methods via POST /rpc.
This module constructs fusion-plugins-ecosystem.MCPHandler, injects fusion-cowork runtime handles,
and routes Studio's JSON-RPC requests to the upstream handler.

Design:
- Lazy loading: MCPHandler is constructed on first /rpc request, avoiding startup overhead in environments without the plugins dependency
- Degradation: when fusion-plugins-ecosystem is not installed, /rpc returns -32603 + an actionable hint
- Node discovery: DeskRuntime injects NodeRegistry, desk.list_nodes() returns cowork-registered nodes
"""

from __future__ import annotations

import logging
import traceback
from typing import Any, Dict, Optional

from fusion_cowork.observability.trace import get_trace_id

logger = logging.getLogger(__name__)

# plugins/* method whitelist (the 15 methods called by Studio PluginBridge.swift)
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

# HI-4: per-method required params fields (method→[required_keys]); missing → rejected with -32602, never reaches the handler
# Key names aligned with upstream MCPHandler: state.get/install/uninstall use plugin_id; config.set requires ≥1 key
_PARAMS_REQUIRED: Dict[str, frozenset] = {
    "plugins/install": frozenset({"plugin_id"}),
    "plugins/uninstall": frozenset({"plugin_id"}),
    "plugins/state.get": frozenset({"plugin_id"}),
}

_HANDLER: Optional[Any] = None
_DESK_RUNTIME: Optional[Any] = None
_LIFECYCLE: Optional[Any] = None
_DEFAULTS_MOUNTED: bool = False


def _build_desk_runtime() -> Any:
    """Construct DeskRuntime, injecting fusion-cowork runtime handles.

    issue #79: with cluster opt-in (FUSION_CLUSTER_ENABLED=1), the handles are wrapped with
    ClusterNodeRegistry / ClusterTaskScheduler so the plugin ecosystem is visible across nodes and tasks dispatch cross-node.
    Default OFF → injects bare NodeRegistry + TaskScheduler (zero behavior change).
    """
    from fusion_plugins_ecosystem.desk_runtime import DeskRuntime

    from ..engine.node import NodeRegistry
    from ..engine.scheduler import TaskScheduler

    try:
        from ..ai.mlx_client import FusionMLXClient

        mlx_client = FusionMLXClient()
    except Exception as e:
        logger.warning("rpc_bridge: FusionMLXClient construction failed, degrading to None: %s", e)
        mlx_client = None

    node_registry = NodeRegistry
    task_scheduler = TaskScheduler()

    from ..distributed_state import get_cluster_state_store

    store = get_cluster_state_store()
    if store is not None:
        from ..distributed_state import ClusterNodeRegistry, ClusterTaskScheduler

        node_registry = ClusterNodeRegistry(NodeRegistry, store)
        task_scheduler = ClusterTaskScheduler(TaskScheduler(), store)
        logger.info("rpc_bridge: cluster mode enabled, injecting cluster-aware handle wrappers node=%s", store.node_id)

    desk = DeskRuntime(
        node_registry=node_registry,
        task_scheduler=task_scheduler,
        mlx_client=mlx_client,
    )
    logger.info("rpc_bridge: DeskRuntime constructed, injected NodeRegistry + TaskScheduler")
    return desk


def get_plugins_handler() -> Any:
    """Lazily load the MCPHandler singleton (with cowork runtime injected).

    Returns a fusion-plugins-ecosystem.MCPHandler instance.
    Raises ImportError when dependencies are missing; the caller converts it to a JSON-RPC error.

    Construction calls register_builtin() to register built-in plugins (caveman_compress etc.),
    making them discoverable via plugins/list. enable for default_mounted plugins is deferred to
    the first dispatch_rpc (see _mount_defaults), because enable is async.
    """
    global _HANDLER, _DESK_RUNTIME, _LIFECYCLE
    if _HANDLER is not None:
        return _HANDLER

    from fusion_plugins_ecosystem.config import EcosystemConfig
    from fusion_plugins_ecosystem.jsonrpc import MCPHandler
    from fusion_plugins_ecosystem.lifecycle import PluginLifecycle
    from fusion_plugins_ecosystem.registry import PluginRegistry

    config = EcosystemConfig()
    _DESK_RUNTIME = _build_desk_runtime()
    registry = PluginRegistry(desk=_DESK_RUNTIME)
    registry.register_builtin()
    _LIFECYCLE = PluginLifecycle(registry)
    _HANDLER = MCPHandler(
        registry=registry,
        desk=_DESK_RUNTIME,
        lifecycle=_LIFECYCLE,
        config=config,
    )
    builtin_count = len(registry.list())
    logger.info(
        "rpc_bridge: MCPHandler constructed (plugins/* delegation ready, %d built-ins)",
        builtin_count,
    )
    return _HANDLER


async def _mount_defaults() -> None:
    """Lazily triggered on first dispatch: enable all default_mounted plugins.

    MCP semantics require tools/list-exposed tools to be directly callable, but lifecycle.execute
    gates on state==ENABLED. register_builtin only registers without enabling, so default plugins
    (caveman_compress) need explicit load+enable. Skipped when config.default_mount_compressor
    is off.
    """
    global _DEFAULTS_MOUNTED
    if _DEFAULTS_MOUNTED or _LIFECYCLE is None or _HANDLER is None:
        return
    _DEFAULTS_MOUNTED = True
    config = _HANDLER.config
    if not getattr(config, "default_mount_compressor", False):
        return
    registry = _HANDLER.registry
    for manifest in registry.default_mounted():
        if manifest.id not in _LIFECYCLE._instances:
            try:
                await _LIFECYCLE.enable(manifest.id)
                logger.info("rpc_bridge: auto-mount %s enabled", manifest.id)
            except Exception as e:
                logger.warning("rpc_bridge: auto-mount %s failed: %s", manifest.id, e)


def is_plugins_available() -> bool:
    """Check whether fusion-plugins-ecosystem is importable (without constructing the handler)."""
    try:
        import fusion_plugins_ecosystem  # noqa: F401

        return True
    except ImportError:
        return False


async def dispatch_rpc(request: Dict[str, Any]) -> Dict[str, Any]:
    """Dispatch an /rpc JSON-RPC request to MCPHandler.

    Args:
        request: full JSON-RPC 2.0 request dict

    Returns:
        JSON-RPC 2.0 response dict (with result or error)
    """
    request_id = request.get("id")
    method = request.get("method", "")

    # LO-10: explicit dispatch — whitelisted methods or plugins/* prefix go through validation+dispatch, others get -32601
    is_plugins_method = method in PLUGINS_METHODS or method.startswith("plugins/")
    if not is_plugins_method:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {
                "code": -32601,
                "message": f"Method not found: {method} (/rpc only hosts plugins/*)",
            },
        }

    # HI-4: validate params shape before handling — missing required fields rejected with -32602, malformed requests never fed to the handler
    required = _PARAMS_REQUIRED.get(method)
    if required is not None:
        params = request.get("params")
        if not isinstance(params, dict):
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32602, "message": "params missing or not an object", "data": {"method": method}},
            }
        missing = [k for k in required if not params.get(k)]
        if missing:
            logger.warning("rpc_bridge: %s missing required params %s", method, missing)
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {
                    "code": -32602,
                    "message": "params validation failed",
                    "data": {"method": method, "missing": missing},
                },
            }

    try:
        handler = get_plugins_handler()
    except ImportError as e:
        logger.error("rpc_bridge: fusion-plugins-ecosystem unavailable: %s", e)
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {
                "code": -32603,
                "message": ("plugins runtime not installed: pip install fusion-plugins-ecosystem"),
            },
        }

    # Lazily trigger default plugin mounting on first dispatch (caveman_compress etc.), making tools/call callable
    await _mount_defaults()

    try:
        response = await handler.handle(request)
        if response is None:
            return {"jsonrpc": "2.0", "result": None, "id": request_id}
        return response
    except Exception as e:
        # HI-5: str(e) not leaked into the RPC response; server-side log only + trace_id returned for troubleshooting
        trace_id = get_trace_id()
        logger.error(
            "rpc_bridge: dispatch exception trace_id=%s method=%s err=%s\n%s",
            trace_id,
            method,
            e,
            traceback.format_exc(),
        )
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {
                "code": -32603,
                "message": "Internal error",
                "data": {"trace_id": trace_id},
            },
        }


def list_nodes_for_plugins() -> list:
    """Node discovery bridge used by desk.list_nodes() (issue #48 point 3).

    Returns the result of cowork NodeRegistry.list(); returns an empty list when dependencies are missing.
    """
    if _DESK_RUNTIME is None or _DESK_RUNTIME.node_registry is None:
        try:
            from ..engine.node import NodeRegistry

            return NodeRegistry.list()
        except Exception as e:
            logger.warning("rpc_bridge: node discovery failed: %s", e)
            return []
    return _DESK_RUNTIME.node_registry.list()
