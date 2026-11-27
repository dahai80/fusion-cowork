"""Authorized working-folder sandbox — Claude Cowork's first security feature.

Nodes may only access explicitly authorized folders (scoped folders); out-of-bounds paths
are refused outright.
Corresponds to audit P0-2: missing scoped folder jail.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

_singleton_lock = threading.Lock()


class ScopedFolderManager:
    """Working-folder whitelist + out-of-bounds path refusal.

    enforce=False intercepts nothing (back-compat); enforce=True requires every path accessed
    by file/shell nodes to resolve() inside one of the authorized folders.
    """

    def __init__(self, scoped_folders: Optional[List[str]] = None, enforce: bool = False):
        self._scopes: List[Path] = []
        for f in scoped_folders or []:
            if f:
                self._scopes.append(Path(f).expanduser().resolve())
        self._enforce = enforce and bool(self._scopes)
        if self._enforce:
            logger.info(f"ScopedFolder sandbox enabled: {len(self._scopes)} authorized folders")

    @classmethod
    def from_config(cls) -> ScopedFolderManager:
        from ..config_center import ConfigCenter

        cc = ConfigCenter.get_instance()
        raw = cc.get("workspace.scoped_folder", "")
        folders = [s.strip() for s in str(raw).split(",") if s.strip()]
        enforce = bool(cc.get("workspace.enforce_scope", False))
        return cls(scoped_folders=folders, enforce=enforce)

    @property
    def enforce(self) -> bool:
        return self._enforce

    @property
    def scopes(self) -> List[Path]:
        return list(self._scopes)

    def is_allowed(self, path: str | os.PathLike) -> bool:
        if not self._enforce:
            return True
        try:
            # LO-9: strict=False does not follow symlinks, then lstat rejects symlink traversal (TOCTOU)
            target = Path(path).expanduser().resolve(strict=False)
        except (OSError, ValueError) as e:
            logger.warning(f"ScopedFolder path resolution failed: {path} ({e})")
            return False
        # LO-9: refuse if any prefix segment of the path is a symlink (prevents in-scope symlinks pointing outside the scope)
        if self._path_contains_symlink(path):
            logger.warning(f"ScopedFolder refusing symlink path: {path} (may escape authorized folders)")
            return False
        for scope in self._scopes:
            try:
                target.relative_to(scope)
                return True
            except ValueError:
                continue
        logger.warning(f"ScopedFolder refusing out-of-bounds path: {target} (not inside an authorized folder)")
        return False

    @staticmethod
    def _path_contains_symlink(path: str | os.PathLike) -> bool:
        # Check every parent directory of path for symlinks segment by segment; no follow, lstat only
        p = Path(path).expanduser()
        try:
            current = p if p.is_absolute() else Path.cwd() / p
        except OSError:
            return True
        seen: set = set()
        while current not in seen:
            seen.add(current)
            try:
                if current.is_symlink():
                    return True
            except OSError:
                return True
            if current.parent == current:
                break
            current = current.parent
        return False

    def ensure_allowed(self, path: str | os.PathLike) -> bool:
        if self.is_allowed(path):
            return True
        logger.error(f"ScopedFolder sandbox blocked: {path}")
        return False

    def resolve_in_scope(self, path: str | os.PathLike) -> Optional[Path]:
        if not self.is_allowed(path):
            return None
        return Path(path).expanduser().resolve()


_default_manager: Optional[ScopedFolderManager] = None


def get_scoped_folder_manager() -> ScopedFolderManager:
    # LO-8: lock around singleton rebuild, prevents from_config races creating multiple instances
    global _default_manager
    if _default_manager is not None:
        return _default_manager
    with _singleton_lock:
        if _default_manager is None:
            _default_manager = ScopedFolderManager.from_config()
        return _default_manager


def reset_scoped_folder_manager() -> None:
    global _default_manager
    with _singleton_lock:
        _default_manager = None
