"""授权工作文件夹沙箱 — Claude Cowork 第一安全特征。

只允许节点访问明确授权的文件夹 (scoped folder)，越界路径直接拒绝。
对应审计 P0-2: scoped folder jail 缺失。
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
            # LO-9: strict=False 不跟随符号链接, 再 lstat 拒符号链接遍历 (TOCTOU)
            target = Path(path).expanduser().resolve(strict=False)
        except (OSError, ValueError) as e:
            logger.warning(f"ScopedFolder 路径解析失败: {path} ({e})")
            return False
        # LO-9: 路径任一前缀段为符号链接则拒 (防 scope 内 symlink 指向 scope 外)
        if self._path_contains_symlink(path):
            logger.warning(f"ScopedFolder 拒符号链接路径: {path} (可能逃逸授权文件夹)")
            return False
        for scope in self._scopes:
            try:
                target.relative_to(scope)
                return True
            except ValueError:
                continue
        logger.warning(f"ScopedFolder 拒绝越界路径: {target} (不在授权文件夹内)")
        return False

    @staticmethod
    def _path_contains_symlink(path: str | os.PathLike) -> bool:
        # 逐段检查 path 的每个父目录是否为符号链接; 不 follow, 仅 lstat
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
        logger.error(f"ScopedFolder 沙箱拦截: {path}")
        return False

    def resolve_in_scope(self, path: str | os.PathLike) -> Optional[Path]:
        if not self.is_allowed(path):
            return None
        return Path(path).expanduser().resolve()


_default_manager: Optional[ScopedFolderManager] = None


def get_scoped_folder_manager() -> ScopedFolderManager:
    # LO-8: 单例重建加锁, 防 from_config 竞态致多实例
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
