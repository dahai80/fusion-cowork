from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

CAPABILITY_TYPES = ("skill", "connector", "subagent")
DEFAULT_WORKSPACE = "default"
MAX_AUDIT = 100
DEFAULT_CAPABILITIES_PATH = Path.home() / ".fusion-cowork" / "capabilities.json"


def normalize_workspace(workspace_id: Any) -> str:
    raw = str(workspace_id or "").strip()
    return raw or DEFAULT_WORKSPACE


@dataclass
class UsageStats:
    count: int = 0
    last_used_at: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"count": int(self.count), "last_used_at": self.last_used_at}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> UsageStats:
        return cls(
            count=int(data.get("count", 0) or 0),
            last_used_at=data.get("last_used_at"),
        )


@dataclass
class CapabilityEntry:
    id: str
    type: str
    name: str = ""
    source: str = ""
    installed_at: float = field(default_factory=time.time)
    enabled: bool = True
    installed_by: str = ""
    granted_scopes: List[str] = field(default_factory=list)
    spawn_policy: Dict[str, Any] = field(default_factory=dict)
    usage: UsageStats = field(default_factory=UsageStats)
    audit: List[Dict[str, Any]] = field(default_factory=list)
    credentials: Dict[str, str] = field(default_factory=dict)

    def to_storage(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "name": self.name,
            "source": self.source,
            "installed_at": self.installed_at,
            "enabled": self.enabled,
            "installed_by": self.installed_by,
            "granted_scopes": list(self.granted_scopes),
            "spawn_policy": dict(self.spawn_policy),
            "usage": self.usage.to_dict(),
            "audit": list(self.audit),
            "credentials": dict(self.credentials),
        }

    @classmethod
    def from_storage(cls, data: Dict[str, Any]) -> CapabilityEntry:
        return cls(
            id=str(data.get("id", "")),
            type=str(data.get("type", "")),
            name=str(data.get("name", "")),
            source=str(data.get("source", "")),
            installed_at=float(data.get("installed_at") or 0.0),
            enabled=bool(data.get("enabled", True)),
            installed_by=str(data.get("installed_by", "")),
            granted_scopes=[str(s) for s in data.get("granted_scopes", []) or []],
            spawn_policy=dict(data.get("spawn_policy") or {}),
            usage=UsageStats.from_dict(data.get("usage") or {}),
            audit=list(data.get("audit") or []),
            credentials=dict(data.get("credentials") or {}),
        )

    def to_public(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "name": self.name,
            "source": self.source,
            "installed_at": self.installed_at,
            "enabled": self.enabled,
            "installed_by": self.installed_by,
            "granted_scopes": list(self.granted_scopes),
            "spawn_policy": dict(self.spawn_policy),
            "usage": self.usage.to_dict(),
            "audit_count": len(self.audit),
            "connected": bool(self.credentials),
        }


class CapabilityStore:
    def __init__(self, path: Optional[Path] = None):
        env_path = os.environ.get("FUSION_COWORK_CAPABILITIES_PATH", "").strip()
        resolved = path or (Path(env_path) if env_path else DEFAULT_CAPABILITIES_PATH)
        self._path = Path(resolved)
        self._lock = threading.RLock()
        self._data: Dict[str, Any] = self._load()
        logger.info(f"能力池存储: {self._path} (工作区 {len(self._data.get('workspaces', {}))} 个)")

    @property
    def path(self) -> Path:
        return self._path

    def _load(self) -> Dict[str, Any]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"version": 1, "workspaces": {}}
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"能力池文件损坏, 重建空池 {self._path}: {e}")
            return {"version": 1, "workspaces": {}}
        if not isinstance(raw, dict) or not isinstance(raw.get("workspaces"), dict):
            logger.warning(f"能力池结构非法, 重建空池 {self._path}")
            return {"version": 1, "workspaces": {}}
        raw.setdefault("version", 1)
        return raw

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self._path)

    def _workspace(self, workspace_id: Any) -> Dict[str, Dict[str, Any]]:
        ws = normalize_workspace(workspace_id)
        spaces = self._data.setdefault("workspaces", {})
        bucket = spaces.setdefault(ws, {})
        for kind in CAPABILITY_TYPES:
            bucket.setdefault(kind, {})
        return bucket

    def _entry(self, workspace_id: Any, cap_type: str, cap_id: str) -> Optional[CapabilityEntry]:
        bucket = self._workspace(workspace_id)
        raw = bucket.get(cap_type, {}).get(cap_id)
        return CapabilityEntry.from_storage(raw) if isinstance(raw, dict) else None

    def _put(self, workspace_id: Any, entry: CapabilityEntry) -> None:
        bucket = self._workspace(workspace_id)
        bucket[entry.type][entry.id] = entry.to_storage()
        self._save()

    def list_entries(self, workspace_id: Any, cap_type: str = "") -> List[CapabilityEntry]:
        bucket = self._workspace(workspace_id)
        kinds = [cap_type] if cap_type in CAPABILITY_TYPES else list(CAPABILITY_TYPES)
        out: List[CapabilityEntry] = []
        with self._lock:
            for kind in kinds:
                for raw in bucket.get(kind, {}).values():
                    if isinstance(raw, dict):
                        out.append(CapabilityEntry.from_storage(raw))
        return sorted(out, key=lambda e: e.id)

    def get(self, workspace_id: Any, cap_type: str, cap_id: str) -> Optional[CapabilityEntry]:
        with self._lock:
            return self._entry(workspace_id, cap_type, cap_id)

    def install(
        self,
        workspace_id: Any,
        cap_type: str,
        cap_id: str,
        *,
        name: str = "",
        source: str = "",
        spawn_policy: Optional[Dict[str, Any]] = None,
        actor: str = "",
    ) -> CapabilityEntry:
        if cap_type not in CAPABILITY_TYPES:
            raise ValueError(f"非法能力类型: {cap_type}")
        if not cap_id:
            raise ValueError("capability id 不能为空")
        with self._lock:
            existing = self._entry(workspace_id, cap_type, cap_id)
            entry = existing or CapabilityEntry(id=cap_id, type=cap_type)
            if not existing:
                entry.installed_at = time.time()
                entry.installed_by = actor
            entry.name = name or entry.name or cap_id
            entry.source = source or entry.source
            if spawn_policy is not None:
                entry.spawn_policy = dict(spawn_policy)
            entry.enabled = True
            entry.audit.append(
                {
                    "at": time.time(),
                    "action": "install" if not existing else "reinstall",
                    "actor": actor,
                }
            )
            entry.audit = entry.audit[-MAX_AUDIT:]
            self._put(workspace_id, entry)
            logger.info(f"能力安装: ws={normalize_workspace(workspace_id)} {cap_id} (actor={actor or '-'})")
            return entry

    def uninstall(self, workspace_id: Any, cap_type: str, cap_id: str, *, actor: str = "") -> bool:
        with self._lock:
            bucket = self._workspace(workspace_id)
            removed = bucket.get(cap_type, {}).pop(cap_id, None) is not None
            if removed:
                self._save()
                logger.info(f"能力卸载: ws={normalize_workspace(workspace_id)} {cap_id} (actor={actor or '-'})")
            return removed

    def set_enabled(
        self, workspace_id: Any, cap_type: str, cap_id: str, enabled: bool, *, actor: str = ""
    ) -> Optional[CapabilityEntry]:
        with self._lock:
            entry = self._entry(workspace_id, cap_type, cap_id)
            if entry is None:
                return None
            entry.enabled = bool(enabled)
            entry.audit.append({"at": time.time(), "action": "enable" if enabled else "disable", "actor": actor})
            entry.audit = entry.audit[-MAX_AUDIT:]
            self._put(workspace_id, entry)
            logger.info(f"能力开关: ws={normalize_workspace(workspace_id)} {cap_id} enabled={entry.enabled}")
            return entry

    def grant_scopes(
        self, workspace_id: Any, cap_id: str, scopes: List[str], *, actor: str = ""
    ) -> Optional[CapabilityEntry]:
        with self._lock:
            entry = self._entry(workspace_id, "connector", cap_id)
            if entry is None:
                return None
            merged = sorted(set(entry.granted_scopes) | {str(s) for s in scopes if s})
            entry.granted_scopes = merged
            entry.audit.append({"at": time.time(), "action": "scope_grant", "actor": actor, "scopes": merged})
            entry.audit = entry.audit[-MAX_AUDIT:]
            self._put(workspace_id, entry)
            logger.info(f"连接器 scope 授权: ws={normalize_workspace(workspace_id)} {cap_id} scopes={merged}")
            return entry

    def set_credentials(self, workspace_id: Any, cap_id: str, credentials: Dict[str, str]) -> bool:
        with self._lock:
            entry = self._entry(workspace_id, "connector", cap_id)
            if entry is None:
                return False
            entry.credentials = dict(credentials)
            entry.audit.append({"at": time.time(), "action": "oauth_connected", "actor": ""})
            entry.audit = entry.audit[-MAX_AUDIT:]
            self._put(workspace_id, entry)
            logger.info(
                f"连接器凭据已保存: ws={normalize_workspace(workspace_id)} {cap_id} "
                f"keys={sorted(credentials)} (值不落日志)"
            )
            return True

    def clear_credentials(self, workspace_id: Any, cap_id: str) -> bool:
        with self._lock:
            entry = self._entry(workspace_id, "connector", cap_id)
            if entry is None:
                return False
            entry.credentials = {}
            entry.granted_scopes = []
            entry.audit.append({"at": time.time(), "action": "oauth_revoked", "actor": ""})
            entry.audit = entry.audit[-MAX_AUDIT:]
            self._put(workspace_id, entry)
            logger.info(f"连接器凭据已撤销: ws={normalize_workspace(workspace_id)} {cap_id}")
            return True

    def record_usage(
        self,
        workspace_id: Any,
        cap_type: str,
        cap_id: str,
        *,
        run_id: str = "",
        workflow: str = "",
        node: str = "",
        detail: str = "",
        verdict: str = "",
        actor: str = "",
    ) -> bool:
        with self._lock:
            entry = self._entry(workspace_id, cap_type, cap_id)
            if entry is None:
                return False
            entry.usage.count += 1
            entry.usage.last_used_at = time.time()
            entry.audit.append(
                {
                    "at": time.time(),
                    "action": "call",
                    "run_id": run_id,
                    "workflow": workflow,
                    "node": node,
                    "detail": detail,
                    "verdict": verdict,
                    "actor": actor,
                }
            )
            entry.audit = entry.audit[-MAX_AUDIT:]
            self._put(workspace_id, entry)
            return True

    def usage(self, workspace_id: Any, cap_type: str, cap_id: str) -> Optional[Dict[str, Any]]:
        entry = self.get(workspace_id, cap_type, cap_id)
        if entry is None:
            return None
        return {
            "id": entry.id,
            "usage": entry.usage.to_dict(),
            "audit": list(reversed(entry.audit)),
        }

    def has_any(self, workspace_id: Any, cap_type: str) -> bool:
        with self._lock:
            bucket = self._workspace(workspace_id)
            return bool(bucket.get(cap_type))
