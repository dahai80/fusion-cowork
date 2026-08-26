"""集群模型同步 — LAN 内模型清单增量同步 + SHA256 校验 + 网络分区降级。

迁移自 fusion-multi-node/master/cluster_sync.py (issue #61)。归属 fusion-cowork
(工作流编排侧承载模型清单同步)。纯本地/LAN, 不出站云。

自包含安全 helper (内联, 不依赖 fusion-multi-node):
- is_safe_peer_host: SSRF 防护, 拒环回/链路本地/未指定/多播/元数据主机, 放私网
- is_safe_path_segment: 路径段校验, 防 ../ 穿越
- build_safe_url: 出站 URL 构造, 主机走 SSRF, path 走过滤

- ModelManifest: 模型文件清单 + SHA256 哈希
- compute_sync_diff: 仅同步差异文件
- PartitionDetector: 心跳超时检测, 降级单机, 恢复自动同步
- NodeLoadReport: 节点硬件负载报告
- ClusterSyncManager: 同步管理器 (start/stop 生命周期 + 增量同步)
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import logging
import os
import re
import socket
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# SSRF 拒绝主机名 (云元数据端点等), 禁止出站
_SSRF_BLOCKED_HOSTNAMES = frozenset(
    {
        "metadata.google.internal",
        "metadata",
        "169.254.169.254",
        "metadata.azure.com",
        "169.254.169.254.nip.io",
    }
)

# 单文件同步大小上限 (64GB), 防 peer 回灌超大体致内存/磁盘 DoS
_MAX_SYNC_FILE_BYTES = 64 * 1024 * 1024 * 1024


def is_safe_path_segment(value: str) -> bool:
    """路径段安全校验 — 防 ../ 穿越与非法字符。仅允许字母数字 _ - 点。"""
    if not value or len(value) > 128:
        return False
    if "/" in value or "\\" in value or "\x00" in value:
        return False
    if value in (".", ".."):
        return False
    if value.startswith(".") and ".." in value:
        return False
    return bool(re.match(r"^[a-zA-Z0-9_.\-]+$", value))


def is_safe_peer_host(host: str) -> bool:
    """出站对端 SSRF 防护 — 拒环回/链路本地/未指定/多播/元数据, 放私网 (LAN 集群)。"""
    if not host or not isinstance(host, str):
        return False
    host = host.strip()
    if not host:
        return False
    if "@" in host or "/" in host or "?" in host or "#" in host:
        return False
    if host.lower() in _SSRF_BLOCKED_HOSTNAMES:
        logger.warning(f"SSRF 拦截: 元数据主机名 {host!r}")
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        if ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast or ip.is_reserved:
            logger.warning(f"SSRF 拦截: 受限 IP {host!r}")
            return False
        if not ip.is_private:
            logger.warning(f"SSRF 拦截: 非私网 IP, 禁止出站公网 {host!r}")
            return False
        return True
    if host.lower() == "localhost":
        logger.warning(f"SSRF 拦截: localhost {host!r}")
        return False
    if not re.match(r"^[a-zA-Z0-9._\-]+$", host):
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        logger.warning(f"SSRF 防护: 无法解析主机名 {host!r}")
        return False
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast or ip.is_reserved:
            logger.warning(f"SSRF 拦截: {host!r} 解析到受限 IP {addr!r}")
            return False
        if not ip.is_private:
            logger.warning(f"SSRF 拦截: {host!r} 解析到公网 IP {addr!r}, 禁止出站")
            return False
    return True


def build_safe_url(scheme: str, host: str, port: int, path: str) -> str:
    """出站 URL — 主机走 SSRF, path 走过滤 (拒绝 .. 穿越)。"""
    if not is_safe_peer_host(host):
        raise ValueError(f"不安全的对端主机: {host!r}")
    if "/.." in path or path == ".." or path.startswith("../") or "/../" in path:
        raise ValueError(f"不安全的 path (含穿越): {path!r}")
    if not re.match(r"^/[a-zA-Z0-9._\-/]*$", path):
        raise ValueError(f"不安全的 path: {path!r}")
    return f"{scheme}://{host}:{int(port)}{path}"


def _safe_rel_path(rel: str) -> str:
    """校验远端 manifest 相对路径, 防穿越。规范化后须仍在 model_dir 内。"""
    if not rel or "\x00" in rel:
        raise ValueError(f"非法同步路径: {rel!r}")
    if rel.startswith("/") or ":" in rel.split("/")[0]:
        raise ValueError(f"非法同步路径: {rel!r}")
    norm = os.path.normpath(rel)
    if norm.startswith("..") or "/.." in norm or norm == "..":
        raise ValueError(f"路径穿越被拒: {rel!r}")
    for seg in norm.split("/"):
        if not is_safe_path_segment(seg):
            raise ValueError(f"路径段非法: {seg!r} (in {rel!r})")
    return norm


class NodeHealth(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    DISCONNECTED = "disconnected"
    SYNCING = "syncing"


class PartitionState(StrEnum):
    CONNECTED = "connected"
    PARTIAL = "partial"
    PARTITIONED = "partitioned"


@dataclass
class FileEntry:
    path: str
    size: int = 0
    sha256: str = ""
    modified_at: float = 0.0


@dataclass
class ModelManifest:
    model_name: str
    model_id: str = ""
    files: list[FileEntry] = field(default_factory=list)
    total_size: int = 0
    created_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_name": self.model_name,
            "model_id": self.model_id,
            "total_size": self.total_size,
            "created_at": self.created_at,
            "files": [
                {"path": f.path, "size": f.size, "sha256": f.sha256, "modified_at": f.modified_at} for f in self.files
            ],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ModelManifest:
        files = [FileEntry(**f) for f in data.get("files", [])]
        return cls(
            model_name=data.get("model_name", ""),
            model_id=data.get("model_id", ""),
            files=files,
            total_size=data.get("total_size", 0),
            created_at=data.get("created_at", 0.0),
        )


@dataclass
class NodeLoadReport:
    node_id: str
    gpu_memory_used_gb: float = 0.0
    gpu_memory_total_gb: float = 0.0
    ram_used_gb: float = 0.0
    ram_total_gb: float = 0.0
    disk_used_gb: float = 0.0
    disk_total_gb: float = 0.0
    cpu_percent: float = 0.0
    active_tasks: int = 0
    max_tasks: int = 0
    reported_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "gpu_memory_used_gb": self.gpu_memory_used_gb,
            "gpu_memory_total_gb": self.gpu_memory_total_gb,
            "ram_used_gb": self.ram_used_gb,
            "ram_total_gb": self.ram_total_gb,
            "disk_used_gb": self.disk_used_gb,
            "disk_total_gb": self.disk_total_gb,
            "cpu_percent": self.cpu_percent,
            "active_tasks": self.active_tasks,
            "max_tasks": self.max_tasks,
            "reported_at": self.reported_at,
        }


def compute_file_sha256(file_path: str) -> str:
    h = hashlib.sha256()
    try:
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception as e:
        logger.error(f"计算文件哈希失败: {file_path}, {e}")
        return ""


def build_manifest(model_name: str, model_dir: str, model_id: str = "") -> ModelManifest:
    """扫描模型目录, 生成 ModelManifest。"""
    files = []
    total_size = 0
    if not os.path.isdir(model_dir):
        logger.warning(f"模型目录不存在: {model_dir}")
        return ModelManifest(model_name=model_name, model_id=model_id)
    for root, _dirs, filenames in os.walk(model_dir):
        for fname in filenames:
            fpath = os.path.join(root, fname)
            rel_path = os.path.relpath(fpath, model_dir)
            try:
                stat = os.stat(fpath)
                sha256 = compute_file_sha256(fpath)
                files.append(
                    FileEntry(
                        path=rel_path,
                        size=stat.st_size,
                        sha256=sha256,
                        modified_at=stat.st_mtime,
                    )
                )
                total_size += stat.st_size
            except Exception as e:
                logger.error(f"扫描文件失败: {fpath}, {e}")
    return ModelManifest(
        model_name=model_name,
        model_id=model_id,
        files=files,
        total_size=total_size,
        created_at=time.time(),
    )


def compute_sync_diff(local: ModelManifest, remote: ModelManifest) -> list[FileEntry]:
    """对比本地与远端 manifest, 返回需同步的文件列表。"""
    local_map = {f.path: f for f in local.files}
    remote_map = {f.path: f for f in remote.files}
    diff_files = []
    for path, remote_entry in remote_map.items():
        local_entry = local_map.get(path)
        if local_entry is None or local_entry.sha256 != remote_entry.sha256:
            diff_files.append(remote_entry)
    for path in local_map:
        if path not in remote_map:
            diff_files.append(FileEntry(path=path, sha256="__deleted__"))
    logger.info(f"增量同步差异: {len(diff_files)}/{len(remote_map)} files need sync")
    return diff_files


class PartitionDetector:
    """网络分区检测与降级管理。"""

    def __init__(
        self,
        node_id: str,
        heartbeat_timeout: float = 30.0,
        check_interval: float = 10.0,
    ):
        self.node_id = node_id
        self.heartbeat_timeout = heartbeat_timeout
        self.check_interval = check_interval
        self._last_heartbeat: dict[str, float] = {}
        self._state = PartitionState.CONNECTED
        self._degraded = False
        self._running = False
        self._task: asyncio.Task | None = None
        self._on_partition: Any = None
        self._on_reconnect: Any = None

    @property
    def state(self) -> PartitionState:
        return self._state

    @property
    def is_degraded(self) -> bool:
        return self._degraded

    def update_heartbeat(self, node_id: str) -> None:
        self._last_heartbeat[node_id] = time.time()
        if self._degraded:
            logger.info(f"分区恢复: 收到 {node_id} 心跳")
            self._degraded = False
            self._state = PartitionState.CONNECTED
            if self._on_reconnect:
                self._on_reconnect()

    def register_callbacks(self, on_partition: Any = None, on_reconnect: Any = None) -> None:
        self._on_partition = on_partition
        self._on_reconnect = on_reconnect

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._check_loop())
        logger.info(f"分区检测启动: timeout={self.heartbeat_timeout}s")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None

    async def _check_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self.check_interval)
            self._check_partition()

    def _check_partition(self) -> None:
        now = time.time()
        disconnected = []
        for nid, last_time in list(self._last_heartbeat.items()):
            if now - last_time > self.heartbeat_timeout:
                disconnected.append(nid)
        if disconnected and not self._degraded:
            self._degraded = True
            self._state = PartitionState.PARTITIONED
            logger.warning(f"网络分区检测: {disconnected} 已断连, 降级单机运行")
            if self._on_partition:
                self._on_partition(disconnected)
        elif not disconnected and self._degraded:
            self._degraded = False
            self._state = PartitionState.CONNECTED
            logger.info("网络分区恢复: 所有节点已重连")
            if self._on_reconnect:
                self._on_reconnect()

    def get_status(self) -> dict[str, Any]:
        now = time.time()
        nodes_status = {}
        for nid, last_time in self._last_heartbeat.items():
            elapsed = now - last_time
            nodes_status[nid] = {
                "last_heartbeat_ago": round(elapsed, 1),
                "status": "disconnected" if elapsed > self.heartbeat_timeout else "connected",
            }
        return {
            "node_id": self.node_id,
            "partition_state": self._state.value,
            "is_degraded": self._degraded,
            "nodes": nodes_status,
        }


class ClusterSyncManager:
    """集群模型同步管理器。"""

    def __init__(
        self,
        model_cache_dir: str = "",
        shared_storage_path: str = "",
        node_id: str = "",
    ):
        self.model_cache_dir = model_cache_dir or os.path.expanduser("~/.fusion-mlx/models")
        self.shared_storage_path = shared_storage_path
        self.node_id = node_id
        self._partition_detector = PartitionDetector(node_id)
        self._sync_queue: asyncio.Queue = asyncio.Queue()
        self._running = False
        self._sync_task: asyncio.Task | None = None

    async def start(self) -> None:
        self._running = True
        self._sync_task = asyncio.create_task(self._sync_loop())
        await self._partition_detector.start()
        logger.info(f"集群同步管理器启动: cache_dir={self.model_cache_dir}")

    async def stop(self) -> None:
        self._running = False
        await self._partition_detector.stop()
        if self._sync_task:
            self._sync_task.cancel()
            self._sync_task = None

    def get_manifest(self, model_name: str) -> ModelManifest:
        """获取本地模型的 manifest。"""
        model_dir = os.path.join(self.model_cache_dir, model_name)
        return build_manifest(model_name, model_dir)

    async def incremental_sync(
        self,
        model_name: str,
        remote_manifest: ModelManifest,
        source_host: str,
        source_port: int = 11452,
    ) -> dict[str, Any]:
        """增量同步: 对比 manifest, 仅下载差异文件。下载后强制 SHA256 校验, 不符即拒写。"""
        local_manifest = self.get_manifest(model_name)
        diff_files = compute_sync_diff(local_manifest, remote_manifest)
        if not diff_files:
            logger.info(f"增量同步: {model_name} 无差异, 跳过")
            return {"model_name": model_name, "synced": 0, "status": "up_to_date"}
        if not is_safe_peer_host(source_host):
            logger.warning(f"增量同步拒绝对端: {source_host!r}")
            return {"model_name": model_name, "synced": 0, "total_diff": len(diff_files), "status": "rejected"}
        if not is_safe_path_segment(model_name):
            logger.warning(f"增量同步拒绝 model_name: {model_name!r}")
            return {"model_name": model_name, "synced": 0, "total_diff": len(diff_files), "status": "rejected"}
        model_dir = os.path.normpath(os.path.join(self.model_cache_dir, model_name))
        synced = 0
        rejected = 0
        for fentry in diff_files:
            if fentry.sha256 == "__deleted__":
                continue
            client = None
            try:
                safe_rel = _safe_rel_path(fentry.path)
                client = httpx.AsyncClient(timeout=300.0)
                url = build_safe_url("http", source_host, source_port, f"/api/models/{model_name}/files")
                resp = await client.get(url, params={"path": safe_rel})
                resp.raise_for_status()
                content = resp.content
                if fentry.size and len(content) != fentry.size:
                    raise ValueError(f"下载大小不符: got {len(content)} != manifest {fentry.size}")
                if len(content) > _MAX_SYNC_FILE_BYTES:
                    raise ValueError(f"下载超大小上限: {len(content)} > {_MAX_SYNC_FILE_BYTES}")
                got_sha = hashlib.sha256(content).hexdigest()
                if got_sha != fentry.sha256:
                    raise ValueError(f"下载完整性校验失败: {got_sha} != manifest {fentry.sha256}")
                dest = os.path.normpath(os.path.join(model_dir, safe_rel))
                if not dest.startswith(model_dir + os.sep) and dest != model_dir:
                    raise ValueError(f"逃逸目标目录: {dest!r}")
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                tmp = dest + ".part"
                with open(tmp, "wb") as f:
                    f.write(content)
                os.replace(tmp, dest)
                synced += 1
            except Exception as e:
                rejected += 1
                logger.error(f"同步文件失败: {fentry.path}, {e}")
            finally:
                if client is not None:
                    await client.aclose()
        logger.info(f"增量同步完成: {model_name}, synced={synced} rejected={rejected}/{len(diff_files)}")
        return {
            "model_name": model_name,
            "synced": synced,
            "rejected": rejected,
            "total_diff": len(diff_files),
            "status": "ok" if rejected == 0 else "partial",
        }

    def trigger_sync(self, model_name: str, source_host: str, source_port: int = 11452) -> None:
        """触发异步同步任务。"""
        self._sync_queue.put_nowait((model_name, source_host, source_port))
        logger.info(f"同步任务入队: {model_name} from {source_host}")

    async def _sync_loop(self) -> None:
        while self._running:
            client = None
            try:
                model_name, source_host, source_port = await asyncio.wait_for(self._sync_queue.get(), timeout=5.0)
            except TimeoutError:
                continue
            try:
                if not is_safe_peer_host(source_host):
                    raise ValueError(f"不安全对端主机: {source_host!r}")
                if not is_safe_path_segment(model_name):
                    raise ValueError(f"非法 model_name: {model_name!r}")
                client = httpx.AsyncClient(timeout=30.0)
                url = build_safe_url("http", source_host, source_port, f"/api/models/{model_name}/manifest")
                resp = await client.get(url)
                resp.raise_for_status()
                remote_manifest = ModelManifest.from_dict(resp.json())
                await self.incremental_sync(model_name, remote_manifest, source_host, source_port)
            except Exception as e:
                logger.error(f"同步循环处理失败: {model_name}, {e}")
            finally:
                if client is not None:
                    await client.aclose()

    def collect_load_report(self) -> NodeLoadReport:
        """采集本节点硬件负载 (psutil 懒加载, 无则返空报告)。"""
        try:
            import psutil

            ram = psutil.virtual_memory()
            disk = psutil.disk_usage("/")
            cpu = psutil.cpu_percent(interval=0.1)
        except ImportError:
            logger.warning("psutil 未安装, 负载报告不可用")
            return NodeLoadReport(node_id=self.node_id, reported_at=time.time())
        return NodeLoadReport(
            node_id=self.node_id,
            ram_used_gb=round(ram.used / (1024**3), 2),
            ram_total_gb=round(ram.total / (1024**3), 2),
            disk_used_gb=round(disk.used / (1024**3), 2),
            disk_total_gb=round(disk.total / (1024**3), 2),
            cpu_percent=cpu,
            reported_at=time.time(),
        )

    def get_cluster_status(self) -> dict[str, Any]:
        return self._partition_detector.get_status()
