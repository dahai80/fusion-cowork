from __future__ import annotations

import importlib
import importlib.util
import logging
import shutil
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..engine.node import BaseNode, NodeRegistry
from .manifest import PluginManifest

logger = logging.getLogger(__name__)


class PluginLoader:
    def __init__(self, plugins_dir: str = ""):
        self._plugins_dir = Path(plugins_dir).expanduser() if plugins_dir else Path.home() / ".fusion-desk" / "plugins"
        self._plugins_dir.mkdir(parents=True, exist_ok=True)
        self._loaded: Dict[str, PluginManifest] = {}
        self._node_map: Dict[str, List[str]] = {}

    def discover(self) -> List[PluginManifest]:
        manifests = []
        for plugin_dir in sorted(self._plugins_dir.iterdir()):
            if not plugin_dir.is_dir():
                continue
            manifest_path = plugin_dir / "manifest.json"
            if not manifest_path.exists():
                logger.debug(f"跳过无清单目录: {plugin_dir.name}")
                continue
            manifest = PluginManifest.from_json(manifest_path)
            if manifest:
                manifests.append(manifest)
        logger.info(f"发现 {len(manifests)} 个插件")
        return manifests

    def load(self, name: str) -> List[BaseNode]:
        plugin_dir = self._plugins_dir / name
        if not plugin_dir.is_dir():
            logger.error(f"插件目录不存在: {name}")
            return []

        manifest_path = plugin_dir / "manifest.json"
        manifest = PluginManifest.from_json(manifest_path)
        if not manifest:
            logger.error(f"插件清单加载失败: {name}")
            return []

        entry_file = plugin_dir / f"{manifest.entry_point}.py"
        if not entry_file.exists():
            logger.error(f"插件入口文件不存在: {entry_file}")
            return []

        module_name = f"fusion_desk_plugin_{name}"
        try:
            spec = importlib.util.spec_from_file_location(module_name, str(entry_file))
            if spec is None or spec.loader is None:
                logger.error(f"无法加载插件模块: {entry_file}")
                return []
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception as e:
            logger.error(f"插件模块执行失败 {name}: {e}")
            return []

        registered = []
        for attr_name in dir(module):
            attr = getattr(module, attr_name, None)
            if attr is None:
                continue
            try:
                if isinstance(attr, type) and issubclass(attr, BaseNode) and attr is not BaseNode:
                    NodeRegistry.register(attr)
                    registered.append(attr)
                    logger.info(f"注册插件节点: {attr.name} ({attr.__name__})")
            except TypeError:
                continue

        self._loaded[name] = manifest
        self._node_map[name] = [n.name for n in registered]
        logger.info(f"插件 {name} 加载完成: {len(registered)} 个节点")
        return registered

    def load_all(self) -> Dict[str, List[BaseNode]]:
        results = {}
        for manifest in self.discover():
            nodes = self.load(manifest.name)
            results[manifest.name] = nodes
        return results

    def unload(self, name: str) -> bool:
        if name not in self._loaded:
            logger.warning(f"插件未加载: {name}")
            return False
        node_names = self._node_map.pop(name, [])
        for node_name in node_names:
            NodeRegistry.unregister(node_name)
            logger.info(f"注销插件节点: {node_name}")
        del self._loaded[name]
        logger.info(f"插件 {name} 已卸载")
        return True

    def install(self, path: str) -> bool:
        src = Path(path).expanduser().resolve()
        if not src.exists():
            logger.error(f"安装源不存在: {path}")
            return False

        if src.suffix == ".zip":
            return self._install_zip(src)
        elif src.is_dir():
            return self._install_dir(src)
        else:
            logger.error(f"不支持的安装源类型: {path}")
            return False

    def _install_zip(self, zip_path: Path) -> bool:
        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                names = zf.namelist()
                top_dirs = set()
                for n in names:
                    parts = n.split("/")
                    if len(parts) > 1:
                        top_dirs.add(parts[0])
                if not top_dirs:
                    logger.error("zip 文件结构无效: 无顶层目录")
                    return False
                plugin_name = top_dirs.pop()
                target = self._plugins_dir / plugin_name
                if target.exists():
                    shutil.rmtree(target)
                zf.extractall(str(self._plugins_dir))
                logger.info(f"zip 插件已安装: {plugin_name}")
                return True
        except zipfile.BadZipFile as e:
            logger.error(f"zip 文件无效: {e}")
            return False
        except Exception as e:
            logger.error(f"zip 安装失败: {e}")
            return False

    def _install_dir(self, src_dir: Path) -> bool:
        manifest_path = src_dir / "manifest.json"
        if not manifest_path.exists():
            logger.error(f"插件目录缺少 manifest.json: {src_dir}")
            return False
        manifest = PluginManifest.from_json(manifest_path)
        if not manifest:
            return False
        target = self._plugins_dir / manifest.name
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(str(src_dir), str(target))
        logger.info(f"插件已安装: {manifest.name} -> {target}")
        return True

    def uninstall(self, name: str) -> bool:
        self.unload(name)
        plugin_dir = self._plugins_dir / name
        if not plugin_dir.exists():
            logger.warning(f"插件目录不存在: {name}")
            return False
        shutil.rmtree(plugin_dir)
        logger.info(f"插件已卸载删除: {name}")
        return True

    def list_plugins(self) -> List[PluginManifest]:
        return list(self._loaded.values())

    def is_loaded(self, name: str) -> bool:
        return name in self._loaded
