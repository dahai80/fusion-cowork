"""授权工作文件夹沙箱 — Claude Cowork 第一安全特征。

只允许节点访问明确授权的文件夹 (scoped folder)，越界路径直接拒绝。
对应审计 P0-2: scoped folder jail 缺失。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)


class ScopedFolderManager:
    """工作文件夹白名单 + 路径越界拒绝。

    enforce=False 时不拦截 (向后兼容); enforce=True 时所有文件/Shell 节点
    访问的路径必须 resolve() 后位于某个授权文件夹内。
    """

    def __init__(self, scoped_folders: Optional[List[str]] = None, enforce: bool = False):
        self._scopes: List[Path] = []
        for f in scoped_folders or []:
            if f:
                self._scopes.append(Path(f).expanduser().resolve())
        self._enforce = enforce and bool(self._scopes)
        if self._enforce:
            logger.info(f"ScopedFolder 沙箱启用: {len(self._scopes)} 个授权文件夹")

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
            target = Path(path).expanduser().resolve()
        except (OSError, ValueError) as e:
            logger.warning(f"ScopedFolder 路径解析失败: {path} ({e})")
            return False
        for scope in self._scopes:
            try:
                target.relative_to(scope)
                return True
            except ValueError:
                continue
        logger.warning(f"ScopedFolder 拒绝越界路径: {target} (不在授权文件夹内)")
        return False

    def ensure_allowed(self, path: str | os.PathLike) -> bool:
        if self.is_allowed(path):
            return True
        logger.error(f"ScopedFolder 沙箱拦截: {path}")
        return False

    def resolve_in_scope(self, path: str | os.PathLike) -> Optional[Path]:
        if not self.is_allowed(path):
            return None
        return Path(path).expanduser().resolve()


_default_manager: Optional[ScopedFolderManager] = None


def get_scoped_folder_manager() -> ScopedFolderManager:
    global _default_manager
    if _default_manager is None:
        _default_manager = ScopedFolderManager.from_config()
    return _default_manager


def reset_scoped_folder_manager() -> None:
    global _default_manager
    _default_manager = None
