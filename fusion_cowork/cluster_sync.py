"""Cluster model sync — LAN model-manifest incremental sync + SHA256 verification + network-partition degradation.

Migrated from fusion-multi-node/master/cluster_sync.py (issue #61). Owned by fusion-cowork
(the workflow-orchestration side carries the model manifest sync). Pure local/LAN, no outbound cloud.

Self-contained security helpers (inlined, no fusion-multi-node dependency):
- is_safe_peer_host: SSRF protection, rejects loopback/link-local/unspecified/multicast/metadata hosts, allows private nets
- is_safe_path_segment: path-segment validation, prevents ../ traversal
- build_safe_url: outbound URL construction, host via SSRF check, path via filtering

- ModelManifest: model file manifest + SHA256 hashes
- compute_sync_diff: sync only differing files
- PartitionDetector: heartbeat-timeout detection, degrade to single machine, auto re-sync on recovery
- NodeLoadReport: node hardware load report
- ClusterSyncManager: sync manager (start/stop lifecycle + incremental sync)
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

# SSRF-rejected hostnames (cloud metadata endpoints etc.), outbound forbidden
_SSRF_BLOCKED_HOSTNAMES = frozenset(
    {
        "metadata.google.internal",
        "metadata",
        "169.254.169.254",
        "metadata.azure.com",
        "169.254.169.254.nip.io",
    }
)

# Per-file sync size cap (64GB), prevents oversized peer payloads from causing memory/disk DoS
_MAX_SYNC_FILE_BYTES = 64 * 1024 * 1024 * 1024


def is_safe_path_segment(value: str) -> bool:
    """Path-segment safety check — prevents ../ traversal and illegal characters. Allows alphanumerics, _ - . only."""
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
    """Outbound peer SSRF protection — rejects loopback/link-local/unspecified/multicast/metadata, allows private nets (LAN cluster)."""
    if not host or not isinstance(host, str):
        return False
    host = host.strip()
    if not host:
        return False
    if "@" in host or "/" in host or "?" in host or "#" in host:
        return False
    if host.lower() in _SSRF_BLOCKED_HOSTNAMES:
        logger.warning(f"SSRF blocked: metadata hostname {host!r}")
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        if ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast or ip.is_reserved:
            logger.warning(f"SSRF blocked: restricted IP {host!r}")
            return False
        if not ip.is_private:
            logger.warning(f"SSRF blocked: non-private IP, outbound to public internet forbidden {host!r}")
            return False
        return True
    if host.lower() == "localhost":
        logger.warning(f"SSRF blocked: localhost {host!r}")
        return False
    if not re.match(r"^[a-zA-Z0-9._\-]+$", host):
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        logger.warning(f"SSRF protection: cannot resolve hostname {host!r}")
        return False
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast or ip.is_reserved:
            logger.warning(f"SSRF blocked: {host!r} resolves to restricted IP {addr!r}")
            return False
        if not ip.is_private:
            logger.warning(f"SSRF blocked: {host!r} resolves to public IP {addr!r}, outbound forbidden")
            return False
    return True


def build_safe_url(scheme: str, host: str, port: int, path: str) -> str:
    """Outbound URL — host via SSRF check, path via filtering (rejects .. traversal)."""
    if not is_safe_peer_host(host):
        raise ValueError(f"不安全的对端主机: {host!r}")  # i18n: user-facing
    if "/.." in path or path == ".." or path.startswith("../") or "/../" in path:
        raise ValueError(f"不安全的 path (含穿越): {path!r}")  # i18n: user-facing
    if not re.match(r"^/[a-zA-Z0-9._\-/]*$", path):
        raise ValueError(f"不安全的 path: {path!r}")  # i18n: user-facing
    return f"{scheme}://{host}:{int(port)}{path}"


def _safe_rel_path(rel: str) -> str:
    """Validate a remote-manifest relative path, preventing traversal. Must stay inside model_dir after normalization."""
    if not rel or "\x00" in rel:
        raise ValueError(f"非法同步路径: {rel!r}")  # i18n: user-facing
    if rel.startswith("/") or ":" in rel.split("/")[0]:
        raise ValueError(f"非法同步路径: {rel!r}")  # i18n: user-facing
    norm = os.path.normpath(rel)
    if norm.startswith("..") or "/.." in norm or norm == "..":
        raise ValueError(f"路径穿越被拒: {rel!r}")  # i18n: user-facing
    for seg in norm.split("/"):
        if not is_safe_path_segment(seg):
            raise ValueError(f"路径段非法: {seg!r} (in {rel!r})")  # i18n: user-facing
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
        logger.error(f"Failed to hash file: {file_path}, {e}")
        return ""


def build_manifest(model_name: str, model_dir: str, model_id: str = "") -> ModelManifest:
    """Scan the model directory and build a ModelManifest."""
    files = []
    total_size = 0
    if not os.path.isdir(model_dir):
        logger.warning(f"Model directory does not exist: {model_dir}")
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
                logger.error(f"Failed to scan file: {fpath}, {e}")
    return ModelManifest(
        model_name=model_name,
        model_id=model_id,
        files=files,
        total_size=total_size,
        created_at=time.time(),
    )


def compute_sync_diff(local: ModelManifest, remote: ModelManifest) -> list[FileEntry]:
    """Compare local and remote manifests, return the list of files needing sync."""
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
    logger.info(f"Incremental sync diff: {len(diff_files)}/{len(remote_map)} files need sync")
    return diff_files


class PartitionDetector:
    """Network partition detection and degradation management."""

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
            logger.info(f"Partition recovered: heartbeat received from {node_id}")
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
        logger.info(f"Partition detection started: timeout={self.heartbeat_timeout}s")

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
            logger.warning(f"Network partition detected: {disconnected} disconnected, degrading to single-machine mode")
            if self._on_partition:
                self._on_partition(disconnected)
        elif not disconnected and self._degraded:
            self._degraded = False
            self._state = PartitionState.CONNECTED
            logger.info("Network partition recovered: all nodes reconnected")
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
    """Cluster model sync manager."""

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
        logger.info(f"Cluster sync manager started: cache_dir={self.model_cache_dir}")

    async def stop(self) -> None:
        self._running = False
        await self._partition_detector.stop()
        if self._sync_task:
            self._sync_task.cancel()
            self._sync_task = None

    def get_manifest(self, model_name: str) -> ModelManifest:
        """Get the local model manifest."""
        model_dir = os.path.join(self.model_cache_dir, model_name)
        return build_manifest(model_name, model_dir)

    async def incremental_sync(
        self,
        model_name: str,
        remote_manifest: ModelManifest,
        source_host: str,
        source_port: int = 11452,
    ) -> dict[str, Any]:
        """Incremental sync: compare manifests, download only differing files. Enforces SHA256 verification after download; mismatch rejects the write."""
        local_manifest = self.get_manifest(model_name)
        diff_files = compute_sync_diff(local_manifest, remote_manifest)
        if not diff_files:
            logger.info(f"Incremental sync: {model_name} no differences, skipping")
            return {"model_name": model_name, "synced": 0, "status": "up_to_date"}
        if not is_safe_peer_host(source_host):
            logger.warning(f"Incremental sync rejected peer: {source_host!r}")
            return {"model_name": model_name, "synced": 0, "total_diff": len(diff_files), "status": "rejected"}
        if not is_safe_path_segment(model_name):
            logger.warning(f"Incremental sync rejected model_name: {model_name!r}")
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
                    raise ValueError(f"下载大小不符: got {len(content)} != manifest {fentry.size}")  # i18n: user-facing
                if len(content) > _MAX_SYNC_FILE_BYTES:
                    raise ValueError(f"下载超大小上限: {len(content)} > {_MAX_SYNC_FILE_BYTES}")  # i18n: user-facing
                got_sha = hashlib.sha256(content).hexdigest()
                if got_sha != fentry.sha256:
                    raise ValueError(f"下载完整性校验失败: {got_sha} != manifest {fentry.sha256}")  # i18n: user-facing
                dest = os.path.normpath(os.path.join(model_dir, safe_rel))
                if not dest.startswith(model_dir + os.sep) and dest != model_dir:
                    raise ValueError(f"逃逸目标目录: {dest!r}")  # i18n: user-facing
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                tmp = dest + ".part"
                with open(tmp, "wb") as f:
                    f.write(content)
                os.replace(tmp, dest)
                synced += 1
            except Exception as e:
                rejected += 1
                logger.error(f"Failed to sync file: {fentry.path}, {e}")
            finally:
                if client is not None:
                    await client.aclose()
        logger.info(f"Incremental sync done: {model_name}, synced={synced} rejected={rejected}/{len(diff_files)}")
        return {
            "model_name": model_name,
            "synced": synced,
            "rejected": rejected,
            "total_diff": len(diff_files),
            "status": "ok" if rejected == 0 else "partial",
        }

    def trigger_sync(self, model_name: str, source_host: str, source_port: int = 11452) -> None:
        """Enqueue an async sync task."""
        self._sync_queue.put_nowait((model_name, source_host, source_port))
        logger.info(f"Sync task enqueued: {model_name} from {source_host}")

    async def _sync_loop(self) -> None:
        while self._running:
            client = None
            try:
                model_name, source_host, source_port = await asyncio.wait_for(self._sync_queue.get(), timeout=5.0)
            except TimeoutError:
                continue
            try:
                if not is_safe_peer_host(source_host):
                    raise ValueError(f"不安全对端主机: {source_host!r}")  # i18n: user-facing
                if not is_safe_path_segment(model_name):
                    raise ValueError(f"非法 model_name: {model_name!r}")  # i18n: user-facing
                client = httpx.AsyncClient(timeout=30.0)
                url = build_safe_url("http", source_host, source_port, f"/api/models/{model_name}/manifest")
                resp = await client.get(url)
                resp.raise_for_status()
                remote_manifest = ModelManifest.from_dict(resp.json())
                await self.incremental_sync(model_name, remote_manifest, source_host, source_port)
            except Exception as e:
                logger.error(f"Sync loop processing failed: {model_name}, {e}")
            finally:
                if client is not None:
                    await client.aclose()

    def collect_load_report(self) -> NodeLoadReport:
        """Collect this node's hardware load (psutil lazy-loaded, empty report when absent)."""
        try:
            import psutil

            ram = psutil.virtual_memory()
            disk = psutil.disk_usage("/")
            cpu = psutil.cpu_percent(interval=0.1)
        except ImportError:
            logger.warning("psutil not installed, load report unavailable")
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
