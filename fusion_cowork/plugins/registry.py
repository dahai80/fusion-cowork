"""Stage 7 — plugin registry (install records + version + signature status + checksum).

Persists to ~/.fusion-cowork/plugins/registry.json (atomic write, reusing the config_center pattern).
register/is_registered/get_version/list_installed/unregister.
Validated at PluginLoader.discover/load time: rejects old versions overwriting newer ones
(rejects unsigned when require_signing is on).
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
            logger.warning(f"Plugin registry read failed {self._file}: {e}, treating as empty")
            self._entries = {}

    def _save(self) -> None:
        data = {"plugins": self._entries}
        # Atomic write: temp + replace (reusing the config_center pattern)
        try:
            fd, tmp = tempfile.mkstemp(dir=str(self._file.parent), suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._file)
        except Exception as e:
            logger.error(f"Plugin registry write failed: {e}")

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
        # Reject old versions overwriting newer ones
        existing = self._entries.get(name)
        if existing:
            old_v = _version_tuple(str(existing.get("version", "0.0.0")))
            new_v = _version_tuple(str(version))
            if new_v < old_v:
                logger.warning(
                    f"Plugin {name} downgrade rejected: {version} < installed {existing.get('version')} (uninstall then reinstall, or upgrade explicitly)"
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
        logger.info(f"Plugin registry registered: {name} v{version} sig={signature_valid}")
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
        logger.info(f"Plugin registry removed: {name}")
        return True


_DEFAULT_REGISTRY: Optional[PluginRegistry] = None


def get_plugin_registry() -> PluginRegistry:
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        _DEFAULT_REGISTRY = PluginRegistry()
        logger.debug("PluginRegistry singleton constructed")
    return _DEFAULT_REGISTRY
