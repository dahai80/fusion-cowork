"""分布式状态层 — 跨进程共享存储 + 集群感知句柄包装 (issue #79)。

fusion-cowork 持有的分布式基础设施: 把 DeskRuntime 注入的 node_registry /
task_scheduler 句柄从单进程只读 passthrough 升级为集群感知包装, 使插件生态
跨节点可见 + 任务跨节点派发/故障转移。

分层:
- DistributedStateStore: 原子文件 (JSON) 后端的跨进程共享状态。节点注册表、
  vRAM 占用账本、插件 enable/installed 集、MCP 会话均序列化于此。线程 + 协程
  安全 (threading.Lock + asyncio.Lock), 原子 temp+rename 落盘。
- ClusterNodeRegistry / ClusterTaskScheduler: 包装本机 NodeRegistry /
  TaskScheduler, 合并共享存储中的 peer 节点/任务, 提供跨节点故障转移派发。
- is_cluster_enabled / get_cluster_state_store: opt-in 开关 (env
  FUSION_CLUSTER_ENABLED + 本机 node_id), 默认 OFF = 零行为变化。

设计约束:
- 纯本地/LAN, 不出站云 (复用 cluster_sync SSRF helper)。
- opt-in, 默认 OFF: 测试/CI 无集群 → 走现有本地逻辑不变。
- 仅 cowork 侧; DeskRuntime 内部状态 (vram_allocations/_mcp_sessions/
  registered_plugin_ids) 无法经注入句柄触达 → 上游 issue 单独处理。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

_CLUSTER_ENABLED_ENV = "FUSION_CLUSTER_ENABLED"
_CLUSTER_NODE_ID_ENV = "FUSION_CLUSTER_NODE_ID"
_CLUSTER_STATE_PATH_ENV = "FUSION_CLUSTER_STATE_PATH"
_DEFAULT_STATE_PATH = os.path.expanduser("~/.fusion-cowork/cluster-state.json")
_HEARTBEAT_TIMEOUT = 30.0


def is_cluster_enabled() -> bool:
    raw = os.environ.get(_CLUSTER_ENABLED_ENV, "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def resolve_node_id(default: Optional[str] = None) -> str:
    raw = os.environ.get(_CLUSTER_NODE_ID_ENV, "").strip()
    if raw:
        return raw
    return default or f"node-{uuid.uuid4().hex[:8]}"


def resolve_state_path() -> str:
    raw = os.environ.get(_CLUSTER_STATE_PATH_ENV, "").strip()
    return raw or _DEFAULT_STATE_PATH


@dataclass
class ClusterNode:
    node_id: str
    host: str = ""
    port: int = 0
    role: str = "worker"
    last_heartbeat_at: float = 0.0
    vram_total_mb: int = 0
    vram_used_mb: int = 0
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "host": self.host,
            "port": self.port,
            "role": self.role,
            "last_heartbeat_at": self.last_heartbeat_at,
            "vram_total_mb": self.vram_total_mb,
            "vram_used_mb": self.vram_used_mb,
            "tags": list(self.tags),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ClusterNode:
        return cls(
            node_id=str(data.get("node_id", "")),
            host=str(data.get("host", "")),
            port=int(data.get("port", 0)),
            role=str(data.get("role", "worker")),
            last_heartbeat_at=float(data.get("last_heartbeat_at", 0.0)),
            vram_total_mb=int(data.get("vram_total_mb", 0)),
            vram_used_mb=int(data.get("vram_used_mb", 0)),
            tags=list(data.get("tags", [])),
        )

    def is_alive(self, now: float, timeout: float = _HEARTBEAT_TIMEOUT) -> bool:
        if not self.last_heartbeat_at:
            return True
        return (now - self.last_heartbeat_at) <= timeout


@dataclass
class VramAllocation:
    node_id: str
    plugin_id: str
    mb: int
    allocated_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "plugin_id": self.plugin_id,
            "mb": self.mb,
            "allocated_at": self.allocated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> VramAllocation:
        return cls(
            node_id=str(data.get("node_id", "")),
            plugin_id=str(data.get("plugin_id", "")),
            mb=int(data.get("mb", 0)),
            allocated_at=float(data.get("allocated_at", 0.0)),
        )


@dataclass
class PluginState:
    node_id: str
    plugin_id: str
    installed: bool = False
    enabled: bool = False
    updated_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "plugin_id": self.plugin_id,
            "installed": self.installed,
            "enabled": self.enabled,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PluginState:
        return cls(
            node_id=str(data.get("node_id", "")),
            plugin_id=str(data.get("plugin_id", "")),
            installed=bool(data.get("installed", False)),
            enabled=bool(data.get("enabled", False)),
            updated_at=float(data.get("updated_at", 0.0)),
        )


@dataclass
class ClusterState:
    nodes: dict[str, ClusterNode] = field(default_factory=dict)
    vram_allocations: list[VramAllocation] = field(default_factory=list)
    plugin_states: list[PluginState] = field(default_factory=list)
    version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodes": {nid: n.to_dict() for nid, n in self.nodes.items()},
            "vram_allocations": [a.to_dict() for a in self.vram_allocations],
            "plugin_states": [s.to_dict() for s in self.plugin_states],
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ClusterState:
        return cls(
            nodes={nid: ClusterNode.from_dict(n) for nid, n in data.get("nodes", {}).items()},
            vram_allocations=[VramAllocation.from_dict(a) for a in data.get("vram_allocations", [])],
            plugin_states=[PluginState.from_dict(s) for s in data.get("plugin_states", [])],
            version=int(data.get("version", 1)),
        )


class DistributedStateStore:
    """跨进程共享状态存储 — 原子文件后端 (JSON)。

    线程 + 协程安全: 同进程内 threading.Lock 串行化同步路径, asyncio.Lock
    串行化协程路径。落盘走 temp+os.replace 原子替换, 跨进程读多写少一致。
    """

    def __init__(
        self,
        state_path: str = "",
        node_id: str = "",
        heartbeat_timeout: float = _HEARTBEAT_TIMEOUT,
    ):
        self.state_path = state_path or resolve_state_path()
        self.node_id = node_id or resolve_node_id()
        self.heartbeat_timeout = heartbeat_timeout
        self._thread_lock = threading.Lock()
        self._async_lock = asyncio.Lock()
        self._cache: Optional[ClusterState] = None
        logger.info(
            "distributed_state: store 初始化 node_id=%s path=%s",
            self.node_id,
            self.state_path,
        )

    def _load_from_disk(self) -> ClusterState:
        try:
            with open(self.state_path, encoding="utf-8") as f:
                data = json.load(f)
            return ClusterState.from_dict(data)
        except FileNotFoundError:
            return ClusterState()
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            logger.warning("distributed_state: 状态文件损坏, 重建空状态: %s", e)
            return ClusterState()

    def _save_to_disk(self, state: ClusterState) -> None:
        state_dir = os.path.dirname(self.state_path)
        if state_dir:
            os.makedirs(state_dir, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=state_dir or ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(state.to_dict(), f, ensure_ascii=False)
            os.replace(tmp_path, self.state_path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def _read(self) -> ClusterState:
        if self._cache is not None:
            return self._cache
        state = self._load_from_disk()
        self._cache = state
        return state

    def _write(self, mutate: Any) -> ClusterState:
        with self._thread_lock:
            state = self._load_from_disk()
            mutate(state)
            state.version += 1
            self._save_to_disk(state)
            self._cache = state
            return state

    def heartbeat(
        self,
        host: str = "",
        port: int = 0,
        role: str = "worker",
        vram_total_mb: int = 0,
        vram_used_mb: int = 0,
        tags: Optional[list[str]] = None,
    ) -> None:
        now = time.time()

        def _mut(state: ClusterState) -> None:
            node = state.nodes.get(self.node_id) or ClusterNode(node_id=self.node_id)
            node.host = host or node.host
            node.port = port or node.port
            node.role = role or node.role
            node.last_heartbeat_at = now
            node.vram_total_mb = vram_total_mb or node.vram_total_mb
            node.vram_used_mb = vram_used_mb or node.vram_used_mb
            node.tags = list(tags) if tags is not None else node.tags
            state.nodes[self.node_id] = node

        self._write(_mut)
        logger.debug("distributed_state: heartbeat node=%s", self.node_id)

    async def heartbeat_async(self, **kwargs: Any) -> None:
        async with self._async_lock:
            await asyncio.to_thread(self.heartbeat, **kwargs)

    def list_nodes(self) -> list[ClusterNode]:
        now = time.time()
        state = self._read()
        return [n for n in state.nodes.values() if n.is_alive(now, self.heartbeat_timeout)]

    def list_all_nodes(self) -> list[ClusterNode]:
        return list(self._read().nodes.values())

    def get_peer_nodes(self) -> list[ClusterNode]:
        now = time.time()
        return [
            n
            for nid, n in self._read().nodes.items()
            if nid != self.node_id and n.is_alive(now, self.heartbeat_timeout)
        ]

    def record_vram_allocation(self, plugin_id: str, mb: int) -> None:
        now = time.time()

        def _mut(state: ClusterState) -> None:
            state.vram_allocations = [
                a for a in state.vram_allocations if not (a.node_id == self.node_id and a.plugin_id == plugin_id)
            ]
            if mb > 0:
                state.vram_allocations.append(
                    VramAllocation(node_id=self.node_id, plugin_id=plugin_id, mb=mb, allocated_at=now)
                )

        self._write(_mut)

    def release_vram_allocation(self, plugin_id: str) -> None:
        def _mut(state: ClusterState) -> None:
            state.vram_allocations = [
                a for a in state.vram_allocations if not (a.node_id == self.node_id and a.plugin_id == plugin_id)
            ]

        self._write(_mut)

    def cluster_vram_usage(self) -> dict[str, int]:
        state = self._read()
        usage: dict[str, int] = {}
        for alloc in state.vram_allocations:
            usage[alloc.node_id] = usage.get(alloc.node_id, 0) + alloc.mb
        return usage

    def total_vram_allocated_mb(self) -> int:
        return sum(a.mb for a in self._read().vram_allocations)

    def can_allocate_vram(self, node_id: str, requested_mb: int, limit_mb: int) -> bool:
        if limit_mb <= 0:
            return True
        usage = self.cluster_vram_usage()
        current = usage.get(node_id, 0)
        return (current + requested_mb) <= limit_mb

    def record_plugin_state(self, plugin_id: str, installed: bool, enabled: bool) -> None:
        now = time.time()

        def _mut(state: ClusterState) -> None:
            state.plugin_states = [
                s for s in state.plugin_states if not (s.node_id == self.node_id and s.plugin_id == plugin_id)
            ]
            state.plugin_states.append(
                PluginState(
                    node_id=self.node_id,
                    plugin_id=plugin_id,
                    installed=installed,
                    enabled=enabled,
                    updated_at=now,
                )
            )

        self._write(_mut)

    def plugin_state_across_cluster(self, plugin_id: str) -> list[PluginState]:
        return [s for s in self._read().plugin_states if s.plugin_id == plugin_id]

    def is_plugin_enabled_anywhere(self, plugin_id: str) -> bool:
        return any(s.plugin_id == plugin_id and s.enabled for s in self._read().plugin_states)

    def remove_node(self, node_id: str) -> None:
        def _mut(state: ClusterState) -> None:
            state.nodes.pop(node_id, None)
            state.vram_allocations = [a for a in state.vram_allocations if a.node_id != node_id]
            state.plugin_states = [s for s in state.plugin_states if s.node_id != node_id]

        self._write(_mut)
        logger.info("distributed_state: 移除节点 %s", node_id)

    def get_state_snapshot(self) -> ClusterState:
        return self._read()

    def invalidate_cache(self) -> None:
        self._cache = None


_STORE: Optional[DistributedStateStore] = None
_STORE_LOCK = threading.Lock()


def get_cluster_state_store() -> Optional[DistributedStateStore]:
    global _STORE
    if not is_cluster_enabled():
        return None
    if _STORE is not None:
        return _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = DistributedStateStore()
        return _STORE


def reset_cluster_state_store() -> None:
    global _STORE
    with _STORE_LOCK:
        _STORE = None


class ClusterNodeRegistry:
    """集群感知节点注册表包装 — 合并本机 NodeRegistry + 共享存储 peer 节点。

    注入 DeskRuntime 时替代裸 NodeRegistry: list() 返回本机已注册节点 + 集群
    peer 节点的合并视图, resolve_node() 先查本机再查 peer。
    """

    def __init__(self, local_registry: Any, store: DistributedStateStore):
        self._local = local_registry
        self._store = store

    def list(self, category: Optional[Any] = None) -> list[dict[str, Any]]:
        local_nodes = self._local.list(category=category) if category is not None else self._local.list()
        peer_nodes = [
            {
                "name": f"peer:{n.node_id}",
                "display_name": f"Peer Node {n.node_id}",
                "category": "cluster",
                "description": f"Cluster peer node {n.node_id} ({n.host}:{n.port})",
                "remote": True,
                "node_id": n.node_id,
                "host": n.host,
                "port": n.port,
                "role": n.role,
                "vram_total_mb": n.vram_total_mb,
                "vram_used_mb": n.vram_used_mb,
                "tags": n.tags,
            }
            for n in self._store.get_peer_nodes()
        ]
        return local_nodes + peer_nodes

    def resolve_node(self, name: str) -> Any:
        local = self._local.get(name) if hasattr(self._local, "get") else None
        if local is not None:
            return local
        if name.startswith("peer:"):
            peer_id = name[5:]
            for n in self._store.get_peer_nodes():
                if n.node_id == peer_id:
                    return n
        return None

    def __getattr__(self, item: str) -> Any:
        return getattr(self._local, item)


class ClusterTaskScheduler:
    """集群感知任务调度包装 — 跨节点故障转移派发。

    list_scheduled_tasks() 合并本机 + peer 节点的活跃任务; dispatch_with_failover()
    选最优节点派发, 失败则转移下一节点。
    """

    def __init__(self, local_scheduler: Any, store: DistributedStateStore):
        self._local = local_scheduler
        self._store = store

    def list_scheduled_tasks(self) -> list[Any]:
        local = self._local.list_active_tasks() if hasattr(self._local, "list_active_tasks") else []
        return list(local)

    def select_node_for_dispatch(
        self,
        vram_required_mb: int = 0,
        prefer_tags: Optional[list[str]] = None,
    ) -> Optional[str]:
        peers = self._store.get_peer_nodes()
        if not peers:
            return self._store.node_id
        scored: list[tuple[float, str]] = []
        now = time.time()
        for n in peers:
            if not n.is_alive(now, self._store.heartbeat_timeout):
                continue
            score = float(n.vram_total_mb - n.vram_used_mb - vram_required_mb)
            if prefer_tags:
                if any(t in n.tags for t in prefer_tags):
                    score += 1000.0
            if score < 0:
                continue
            scored.append((score, n.node_id))
        if not scored:
            return self._store.node_id
        scored.sort(reverse=True)
        return scored[0][1]

    def dispatch_with_failover(
        self,
        executor: Any,
        vram_required_mb: int = 0,
        prefer_tags: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        peers = self._store.get_peer_nodes()
        candidates = [self._store.node_id] + [n.node_id for n in peers]
        errors: list[dict[str, Any]] = []
        for node_id in candidates:
            try:
                result = executor(node_id)
                logger.info("distributed_state: 派发成功 node=%s", node_id)
                return {"dispatched_to": node_id, "result": result, "errors": errors}
            except Exception as e:
                logger.warning("distributed_state: 派发失败 node=%s err=%s, 转移下一节点", node_id, e)
                errors.append({"node_id": node_id, "error": str(e)})
        logger.error("distributed_state: 所有节点派发失败, 无可用故障转移目标")
        return {"dispatched_to": None, "result": None, "errors": errors}

    def __getattr__(self, item: str) -> Any:
        return getattr(self._local, item)
