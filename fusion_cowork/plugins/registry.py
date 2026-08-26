"""Stage 7 — 插件 registry (安装记录 + 版本 + 签名状态 + checksum)。

持久 ~/.fusion-cowork/plugins/registry.json (原子写, 复用 config_center 模式)。
register/is_registered/get_version/list_installed/unregister。
PluginLoader.discover/load 时校验: 拒旧版本覆盖新版本 (require_signing 时拒未签名)。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_REGISTRY_DIR = os.environ.get("FUSION_CONFIG_DIR") or os.path.expanduser("~/.fusion-cowork/plugins")
_REGISTRY_FILE = os.path.join(_REGISTRY_DIR, "registry.json")


def _version_tuple(v: str) -> tuple:
    parts = []
    for p in v.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    return tuple(parts)


class PluginRegistry:
    def __init__(self, registry_file: str = _REGISTRY_FILE):
        self._file = Path(registry_file)
        self._file.parent.mkdir(parents=True, exist_ok=True)
        self._entries: Dict[str, Dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if not self._file.exists():
            return
        try:
            data = json.loads(self._file.read_text(encoding="utf-8"))
            entries = data.get("plugins", {})
            if isinstance(entries, dict):
                self._entries = entries
        except Exception as e:
            logger.warning(f"插件 registry 读取失败 {self._file}: {e}, 视为空")
            self._entries = {}

    def _save(self) -> None:
        data = {"plugins": self._entries}
        # 原子写: temp + replace (复用 config_center 模式)
        try:
            fd, tmp = tempfile.mkstemp(dir=str(self._file.parent), suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._file)
        except Exception as e:
            logger.error(f"插件 registry 写入失败: {e}")

    @staticmethod
    def _checksum(path: Path) -> str:
        h = hashlib.sha256()
        try:
            for chunk in iter(lambda: path.read_bytes() if path.is_file() else b"", b""):
                h.update(chunk)
                break
        except Exception:
            pass
        return h.hexdigest()

    def register(
        self,
        name: str,
        version: str,
        signature_valid: bool,
        checksum: str = "",
        installed_at: str = "",
    ) -> bool:
        # 拒旧版本覆盖新版本
        existing = self._entries.get(name)
        if existing:
            old_v = _version_tuple(str(existing.get("version", "0.0.0")))
            new_v = _version_tuple(str(version))
            if new_v < old_v:
                logger.warning(
                    f"插件 {name} 降版拒绝: {version} < 已装 {existing.get('version')} (卸载后重装或显式升级)"
                )
                return False
        import time

        self._entries[name] = {
            "name": name,
            "version": version,
            "signature_valid": signature_valid,
            "checksum": checksum,
            "installed_at": installed_at or str(int(time.time())),
        }
        self._save()
        logger.info(f"插件 registry 登记: {name} v{version} sig={signature_valid}")
        return True

    def is_registered(self, name: str) -> bool:
        return name in self._entries

    def get_version(self, name: str) -> Optional[str]:
        e = self._entries.get(name)
        return e.get("version") if e else None

    def list_installed(self) -> List[Dict[str, Any]]:
        return list(self._entries.values())

    def unregister(self, name: str) -> bool:
        if name not in self._entries:
            return False
        del self._entries[name]
        self._save()
        logger.info(f"插件 registry 移除: {name}")
        return True


_DEFAULT_REGISTRY: Optional[PluginRegistry] = None


def get_plugin_registry() -> PluginRegistry:
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        _DEFAULT_REGISTRY = PluginRegistry()
        logger.debug("PluginRegistry 单例构造")
    return _DEFAULT_REGISTRY
